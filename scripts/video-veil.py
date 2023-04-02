import copy
import os
import shutil
from tqdm import trange

import cv2
import numpy as np
import gradio as gr
from PIL import Image
from datetime import datetime, timezone

import modules.scripts as scripts
from modules import processing, shared, script_callbacks, images, sd_samplers, sd_samplers_common
from modules.processing import process_images, setup_color_correction
from modules.shared import opts, cmd_opts, state, sd_model

import torch
import k_diffusion as K

color_correction_option_none = 'None'
color_correction_option_video = 'From Source Video'
color_correction_option_generated_image = 'From Stable Diffusion Generated Image'

color_correction_options = [
    color_correction_option_none,
    color_correction_option_video,
    color_correction_option_generated_image,
]

class VideoVeilImage:
    def __init__(self, frame_array: np.ndarray = None, frame_image: Image = None):
        self.frame_array = frame_array
        self.frame_image = frame_image
        self.transformed_image: Image = None

        if frame_array is not None:
            converted_frame_array = cv2.cvtColor(self.frame_array, cv2.COLOR_BGR2RGB)
            self.frame_image = Image.fromarray(converted_frame_array)
        elif frame_image is not None:
            self.frame_array = np.array(self.frame_image)

        # Capture the dimensions of our image
        self.height, self.width, _ = self.frame_array.shape

    def set_transformed_image(self, image: Image):
        self.transformed_image = image

    # Based on changes suggested by briansemrau in https://github.com/AUTOMATIC1111/stable-diffusion-webui/issues/736
    def find_noise_for_image_sigma_adjustment(self, p, cond, uncond, cfg_scale: float, steps: int):
        x = p.init_latent

        s_in = x.new_ones([x.shape[0]])
        if shared.sd_model.parameterization == "v":
            dnw = K.external.CompVisVDenoiser(shared.sd_model)
            skip = 1
        else:
            dnw = K.external.CompVisDenoiser(shared.sd_model)
            skip = 0
        sigmas = dnw.get_sigmas(steps).flip(0)

        shared.state.sampling_steps = steps

        for i in trange(1, len(sigmas)):
            # shared.state.sampling_step += 1

            x_in = torch.cat([x] * 2)
            sigma_in = torch.cat([sigmas[i - 1] * s_in] * 2)
            cond_in = torch.cat([uncond, cond])

            image_conditioning = torch.cat([p.image_conditioning] * 2)
            cond_in = {"c_concat": [image_conditioning], "c_crossattn": [cond_in]}

            c_out, c_in = [K.utils.append_dims(k, x_in.ndim) for k in dnw.get_scalings(sigma_in)[skip:]]

            if i == 1:
                t = dnw.sigma_to_t(torch.cat([sigmas[i] * s_in] * 2))
            else:
                t = dnw.sigma_to_t(sigma_in)

            eps = shared.sd_model.apply_model(x_in * c_in, t, cond=cond_in)
            denoised_uncond, denoised_cond = (x_in + eps * c_out).chunk(2)

            denoised = denoised_uncond + (denoised_cond - denoised_uncond) * cfg_scale

            if i == 1:
                d = (x - denoised) / (2 * sigmas[i])
            else:
                d = (x - denoised) / sigmas[i - 1]

            dt = sigmas[i] - sigmas[i - 1]
            x = x + d * dt

            sd_samplers_common.store_latent(x)

            # This shouldn't be necessary, but solved some VRAM issues
            del x_in, sigma_in, cond_in, c_out, c_in, t,
            del eps, denoised_uncond, denoised_cond, denoised, d, dt

        shared.state.nextjob()

        return x / sigmas[-1]

class VideoVeilSourceVideo:
    def __init__(
            self,
            use_images_directory: bool,
            video_path: str,
            directory_upload_path: str,
            test_run: bool,
            test_run_frames_count: int,
            throw_errors_when_invalid: bool = True,
    ):
        self.frames: list[VideoVeilImage] = []

        self.use_images_directory: bool = use_images_directory
        self.video_path: str = video_path
        self.directory_upload_path: str = directory_upload_path
        self.test_run: bool = test_run
        self.test_run_frames_count: int = None if not test_run else test_run_frames_count
        self.output_video_path: str = None
        self.video_width: int = 0
        self.video_height: int = 0


        if use_images_directory:
            print(f"directory_upload_path: {directory_upload_path}")
            if directory_upload_path is None or not os.path.exists(directory_upload_path):
                if throw_errors_when_invalid:
                    raise Exception(f"Directory not found: '{directory_upload_path}'.")
            else:
                self._load_frames_from_folder()
                self._set_video_dimensions()
        else:
            print(f"video_path: {video_path}")
            if video_path is None or not os.path.exists(video_path):
                if throw_errors_when_invalid:
                    raise Exception(f"Video not found: '{video_path}'.")
            else:
                self._load_frames_from_video()
                self._set_video_dimensions()

    def create_mp4(self, seed: int, output_directory: str, img2img_gallery=None):
        if self.test_run:
            return
        else:
            # get the original file name, and slap a timestamp on it
            original_file_name: str = ""
            fps = 30  # TODO: Add this as an option when they pick a folder
            if not self.use_images_directory:
                original_file_name = os.path.basename(self.video_path)
                clip = cv2.VideoCapture(self.video_path)
                if clip:
                    fps = clip.get(cv2.CAP_PROP_FPS)
                    clip.release()
            else:
                original_file_name = f"{os.path.basename(self.directory_upload_path)}.mp4"

            date_string = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
            file_name = f"{date_string}-{seed}-{original_file_name}"

            output_directory = os.path.join(output_directory, "video-veil-output")
            os.makedirs(output_directory, exist_ok=True)
            output_path = os.path.join(output_directory, file_name)

            print(f"Saving *.mp4 to: {output_path}")

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(output_path, fourcc, fps, (self.video_width, self.video_height))

            # write the images to the video file
            for frame in self.frames:
                out.write(cv2.cvtColor(np.array(frame.transformed_image), cv2.COLOR_RGB2BGR))

            out.release()

            self.output_path = output_path
            if img2img_gallery is not None:
                img2img_gallery.update([output_path])

        return

    def _load_frames_from_folder(self):
        image_extensions = ['.jpg', '.jpeg', '.png']
        image_names = [
            file_name for file_name in os.listdir(self.directory_upload_path)
            if any(file_name.endswith(ext) for ext in image_extensions)
        ]

        if len(image_names) <= 0:
            raise Exception(f"No images (*.png, *.jpg, *.jpeg) found in '{self.directory_upload_path}'.")

        self.frames = []
        # Open the images and convert them to np.ndarray
        for i, image_name in enumerate(image_names):

            if self.test_run_frames_count is not None and len(self.frames) >= self.test_run_frames_count:
                return

            image_path = os.path.join(self.directory_upload_path, image_name)

            # Convert the image
            image = Image.open(image_path)

            if not image.mode == "RGB":
                image = image.convert("RGB")

            self.frames.append(VideoVeilImage(frame_image=image))

        return

    def _load_frames_from_video(self):
        cap = cv2.VideoCapture(self.video_path)
        self.frames = []
        if not cap.isOpened():
            return
        while True:
            if self.test_run_frames_count is not None and len(self.frames) >= self.test_run_frames_count:
                cap.release()
                return

            ret, frame = cap.read()
            if ret:
                self.frames.append(VideoVeilImage(frame_array=frame))
            else:
                cap.release()
                return

        return

    def _set_video_dimensions(self):
        if len(self.frames):
            first_frame = self.frames[0]
            self.video_width, self.video_height = first_frame.width, first_frame.height


class Script(scripts.Script):

    def __init__(self):
        self.source_video: VideoVeilSourceVideo = None
        self.img2img_component = gr.Image()
        self.img2img_gallery = gr.Gallery()
        self.img2img_w_slider = gr.Slider()
        self.img2img_h_slider = gr.Slider()

    def title(self):
        return "Video-Veil"

    def show(self, is_img2img):
        return is_img2img

    # How the script's is displayed in the UI. See https://gradio.app/docs/#components
    # for the different UI components you can use and how to create them.
    # Most UI components can return a value, such as a boolean for a checkbox.
    # The returned values are passed to the run method as parameters.
    def ui(self, is_img2img):

        # Use these later to help guide the user
        # control_net_models_count = opts.data.get("control_net_max_models_num", 1)
        # control_net_allow_script_control = opts.data.get("control_net_allow_script_control", False)

        with gr.Group(elem_id="vv_accordion"):
            with gr.Row():
                gr.HTML("<br /><h1 style='border-bottom: 1px solid #eee;'>Video Veil</h1>")
            with gr.Row():
                gr.HTML("<div><a style='color: #0969da;' href='https://github.com/djbielejeski/video-veil-automatic1111-extension' target='_blank'>Video-Veil Github</a></div>")

            # Input type selection row, allow the user to choose their input type (*.MP4 file or Directory Path)
            with gr.Row():
                use_images_directory_gr = gr.Checkbox(label=f"Use Directory", value=False, elem_id=f"vv_use_directory_for_video", info="Use Directory of images instead of *.mp4 file")
                gr.HTML("<br />")


            # Video Uploader Row
            with gr.Row() as video_uploader_row:
                video_path_gr = gr.Video(format='mp4', source='upload', elem_id=f"vv_video_path")
                gr.HTML("<br />")

            # Directory Path Row
            with gr.Row(visible=False) as directory_uploader_row:
                directory_upload_path_gr = gr.Textbox(
                    label="Directory",
                    value="",
                    elem_id="vv_video_directory",
                    interactive=True,
                    info="Path to directory containing your individual frames for processing."
                )
                gr.HTML("<br />")

            # Video Source Info Row
            with gr.Row():
                video_source_info_gr = gr.HTML("")

            # Color Correction
            with gr.Row():
                color_correction_gr = gr.Dropdown(
                    label="Color Correction",
                    choices=color_correction_options,
                    value=color_correction_option_none,
                    elem_id="vv_color_correction",
                    interactive=True,
                )

            # Test Processing Row
            with gr.Row():
                test_run_gr = gr.Checkbox(label=f"Test Run", value=False, elem_id=f"vv_test_run")
                gr.HTML("<br />")

            with gr.Row(visible=False) as test_run_parameters_row:
                test_run_frames_count_gr = gr.Slider(
                                label="# of frames to test",
                                value=1,
                                minimum=1,
                                maximum=100,
                                step=1,
                                elem_id="vv_test_run_frames_count",
                                interactive=True,
                            )

            # Click handlers and UI Updaters

            # If the user selects a video or directory, update the img2img sections
            def video_src_change(
                    use_directory_for_video: bool,
                    video_path: str,
                    directory_upload_path: str,
            ):
                temp_video = VideoVeilSourceVideo(
                    use_images_directory=use_directory_for_video,
                    video_path=video_path,
                    directory_upload_path=directory_upload_path,
                    test_run=True,
                    test_run_frames_count=1,
                    throw_errors_when_invalid=False
                )

                if len(temp_video.frames) > 0:
                    # Update the img2img settings via the existing Gradio controls
                    first_frame = temp_video.frames[0]

                    return {
                        self.img2img_component: gr.update(value=first_frame.frame_image),
                        self.img2img_w_slider: gr.update(value=first_frame.width),
                        self.img2img_h_slider: gr.update(value=first_frame.height),
                        video_source_info_gr: gr.update(value=f"<div style='color: #333'>Video Frames found: {first_frame.width}x{first_frame.height}px</div>")
                    }
                else:
                    error_message = "" if video_path is None or directory_upload_path is None or directory_upload_path == "" else "Invalid source, unable to parse video frames from input."
                    return {
                        self.img2img_component: gr.update(value=None),
                        self.img2img_w_slider: gr.update(value=512),
                        self.img2img_h_slider: gr.update(value=512),
                        video_source_info_gr: gr.update(value=f"<div style='color: red'>{error_message}</div>")
                    }

            source_inputs = [
                video_path_gr,
                directory_upload_path_gr,
            ]

            for source_input in source_inputs:
                source_input.change(
                    fn=video_src_change,
                    inputs=[
                        use_images_directory_gr,
                        video_path_gr,
                        directory_upload_path_gr,
                    ],
                    outputs=[
                        self.img2img_component,
                        self.img2img_w_slider,
                        self.img2img_h_slider,
                        video_source_info_gr,
                    ]
                )

            # Upload type change
            def change_upload_type_click(
                use_directory_for_video: bool
            ):
                return {
                    video_uploader_row: gr.update(visible=not use_directory_for_video),
                    directory_uploader_row: gr.update(visible=use_directory_for_video),
                }

            use_images_directory_gr.change(
                fn=change_upload_type_click,
                inputs=[
                    use_images_directory_gr
                ],
                outputs=[
                    video_uploader_row,
                    directory_uploader_row
                ]
            )

            # Test run change
            def test_run_click(
                    is_test_run: bool
            ):
                return {
                    test_run_parameters_row: gr.update(visible=is_test_run)
                }

            test_run_gr.change(
                fn=test_run_click,
                inputs=[
                    test_run_gr
                ],
                outputs=[
                    test_run_parameters_row
                ]
            )

        return (
            use_images_directory_gr,
            video_path_gr,
            directory_upload_path_gr,
            color_correction_gr,
            test_run_gr,
            test_run_frames_count_gr,
        )


    # From: https://github.com/LonicaMewinsky/gif2gif/blob/main/scripts/gif2gif.py
    # Grab the img2img image components for update later
    # Maybe there's a better way to do this?
    def after_component(self, component, **kwargs):
        if component.elem_id == "img2img_image":
            self.img2img_component = component
            return self.img2img_component
        if component.elem_id == "img2img_gallery":
            self.img2img_gallery = component
            return self.img2img_gallery
        if component.elem_id == "img2img_width":
            self.img2img_w_slider = component
            return self.img2img_w_slider
        if component.elem_id == "img2img_height":
            self.img2img_h_slider = component
            return self.img2img_h_slider

    def apply_img2img_alternate_fix(
            self,
            cp,
            frame: VideoVeilImage,
            prompt_describing_original_video: str,
            neg_prompt_describing_original_video: str,
    ):
        cp.batch_size = 1
        cp.sampler_name = "Euler"

        def sample_extra(conditioning, unconditional_conditioning, seeds, subseeds, subseed_strength, prompts):
            shared.state.job_count += 1
            decode_cfg_scale = 1.0
            cond = cp.sd_model.get_learned_conditioning([prompt_describing_original_video])
            uncond = cp.sd_model.get_learned_conditioning([neg_prompt_describing_original_video])
            rec_noise = frame.find_noise_for_image_sigma_adjustment(
                p=cp,
                cond=cond,
                uncond=uncond,
                cfg_scale=decode_cfg_scale,
                steps=cp.steps
            )

            sampler = sd_samplers.create_sampler(cp.sampler_name, cp.sd_model)
            sigmas = sampler.model_wrap.get_sigmas(cp.steps)
            noise_dt = rec_noise - (cp.init_latent / sigmas[0])
            # cp.seed = cp.seed + 1

            return sampler.sample_img2img(cp, cp.init_latent, noise_dt, conditioning, unconditional_conditioning, image_conditioning=cp.image_conditioning)

        cp.sample = sample_extra

    """
    This function is called if the script has been selected in the script dropdown.
    It must do all processing and return the Processed object with results, same as
    one returned by processing.process_images.

    Usually the processing is done by calling the processing.process_images function.

    args contains all values returned by components from ui()
    """
    def run(
            self,
            p,
            use_images_directory: bool,
            video_path: str,
            directory_upload_path: str,
            color_correction: str,
            test_run: bool,
            test_run_frames_count: int,
    ):
        no_video_path = video_path is None or video_path == ""
        no_directory_upload_path = directory_upload_path is None or directory_upload_path == ""
        enabled = not no_video_path or not no_directory_upload_path
        if enabled:
            print(f"use_images_directory: {use_images_directory}")

            source_video = VideoVeilSourceVideo(
                use_images_directory=use_images_directory,
                video_path=video_path,
                directory_upload_path=directory_upload_path,
                test_run=test_run,
                test_run_frames_count=test_run_frames_count,
            )

            print(f"color_correction: {color_correction}")
            print(f"test_run: {test_run}")
            print(f"test_run_frames_count: {test_run_frames_count}")
            print(f"# of frames: {len(source_video.frames)}")

            if len(source_video.frames) > 0:
                state.job_count = len(source_video.frames) * p.n_iter
                state.job_no = 0

                # Loop over all the frames and process them
                for i, frame in enumerate(source_video.frames):
                    if state.skipped: state.skipped = False
                    if state.interrupted: break

                    state.job = f"{state.job_no + 1} out of {state.job_count}"

                    cp = copy.copy(p)

                    # Img2Img Alternate fixes - TODO: Lock behind a checkbox
                    self.apply_img2img_alternate_fix(
                        cp=cp,
                        frame=frame,
                        # TODO: Accept these as input
                        prompt_describing_original_video="This prompt should describe your input video",
                        neg_prompt_describing_original_video="This is your negative prompt describing your input video"
                    )


                    # Set the ControlNet reference image
                    cp.control_net_input_image = [frame.frame_array]

                    # Set the Img2Img reference image to the frame of the video
                    cp.init_images = [frame.frame_image]

                    # Color Correction
                    if color_correction == color_correction_option_none:
                        pass
                    elif color_correction == color_correction_option_video:
                        # Use the source video to apply color correction
                        cp.color_corrections = [setup_color_correction(frame.frame_image)]
                    elif color_correction == color_correction_option_generated_image:
                        if len(source_video.frames) > 0 and source_video.frames[-1].transformed_image is not None:
                            # use the previous frame for color correction
                            cp.color_corrections = [setup_color_correction(source_video.frames[-1].transformed_image)]


                    # Process the image via the normal Img2Img pipeline
                    proc = process_images(cp)

                    # Capture the output, we will use this to re-create our video
                    frame.set_transformed_image(proc.images[0])

                    cp.close()

                # Show the user what we generated
                proc.images = [frame.transformed_image for frame in source_video.frames]

                # now create a video
                source_video.create_mp4(seed=proc.seed, output_directory=cp.outpath_samples, img2img_gallery=self.img2img_gallery)

        else:
            proc = process_images(p)

        return proc

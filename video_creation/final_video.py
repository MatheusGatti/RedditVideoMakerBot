import time
import threading
import tempfile
import multiprocessing
import os
import re
import shutil
from os.path import exists  # Needs to be imported specifically
from typing import Final
from typing import Tuple, Any

import ffmpeg
import translators as ts
from PIL import Image
from rich.console import Console
from rich.progress import track

from utils import settings
from utils.cleanup import cleanup
from utils.console import print_step, print_substep
from utils.thumbnail import create_thumbnail
from utils.videos import save_data
from glob import glob

from moviepy.video.io.VideoFileClip import VideoFileClip
from moviepy.audio.io.AudioFileClip import AudioFileClip
from math import ceil


console = Console()


class ProgressFfmpeg(threading.Thread):
    def __init__(self, vid_duration_seconds, progress_update_callback):
        threading.Thread.__init__(self, name="ProgressFfmpeg")
        self.stop_event = threading.Event()
        self.output_file = tempfile.NamedTemporaryFile(mode="w+", delete=False)
        self.vid_duration_seconds = vid_duration_seconds
        self.progress_update_callback = progress_update_callback

    def run(self):
        while not self.stop_event.is_set():
            latest_progress = self.get_latest_ms_progress()
            if latest_progress is not None:
                completed_percent = latest_progress / self.vid_duration_seconds
                self.progress_update_callback(completed_percent)
            time.sleep(1)

    def get_latest_ms_progress(self):
        lines = self.output_file.readlines()

        if lines:
            for line in lines:
                if "out_time_ms" in line:
                    out_time_ms = line.split("=")[1]
                    return int(out_time_ms) / 1000000.0
        return None

    def stop(self):
        self.stop_event.set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args, **kwargs):
        self.stop()


def name_normalize(name: str) -> str:
    name = re.sub(r'[?\\"%*:|<>]', "", name)
    name = re.sub(r"( [w,W]\s?\/\s?[o,O,0])", r" without", name)
    name = re.sub(r"( [w,W]\s?\/)", r" with", name)
    name = re.sub(r"(\d+)\s?\/\s?(\d+)", r"\1 of \2", name)
    name = re.sub(r"(\w+)\s?\/\s?(\w+)", r"\1 or \2", name)
    name = re.sub(r"\/", r"", name)

    lang = settings.config["reddit"]["thread"]["post_lang"]
    if lang:
        print_substep("Translating filename...")
        translated_name = ts.google(name, to_language=lang)
        return translated_name
    else:
        return name


def prepare_background(reddit_id: str, W: int, H: int) -> str:
    output_path = f"assets/temp/{reddit_id}/background_noaudio.mp4"
    output = (
        ffmpeg.input(f"assets/temp/{reddit_id}/background.mp4")
        .filter("crop", f"ih*({W}/{H})", "ih")
        .output(
            output_path,
            an=None,
            **{
                "c:v": "h264",
                "b:v": "20M",
                "b:a": "192k",
                "threads": multiprocessing.cpu_count(),
            },
        )
        .overwrite_output()
    )
    try:
        output.run(quiet=True)
    except Exception as e:
        print(e)
        exit()
    return output_path


# Pegar todos nomes dos post audios e retornar em uma lista
def get_postaudio_inputs(reddit_id):
    folder_path = f"assets/temp/{reddit_id}/mp3"
    filenames = [f for f in os.listdir(
        folder_path) if "postaudio" in f and f.endswith(".mp3")]
    filenames.sort(key=lambda f: int(f[len("postaudio-"):-len(".mp3")]))
    input_args = [ffmpeg.input(os.path.join(folder_path, f))
                  for f in filenames]
    return input_args


# Pegar todos nomes dos comentarios
def get_non_postaudio_inputs(reddit_id):
    folder_path = f"assets/temp/{reddit_id}/mp3/"
    inputs = []
    for filename in os.listdir(folder_path):
        if (
            filename.endswith(".mp3")
            and not any(substring in filename for substring in ["postaudio", "title", "silence"])
            and filename[:-4].isnumeric()
        ):
            input_path = os.path.join(folder_path, filename)
            inputs.append((int(filename[:-4]), ffmpeg.input(input_path)))
    inputs.sort()
    return [input_tuple[1] for input_tuple in inputs]


# Pegar o tempo dos postaudio
def get_total_postaudio_duration(reddit_id):
    mp3_folder = f"assets/temp/{reddit_id}/mp3"
    postaudio_files = [f for f in os.listdir(
        mp3_folder) if "postaudio" in f and f.endswith(".mp3")]

    duration = 0
    for f in postaudio_files:
        file_path = os.path.join(mp3_folder, f)
        probe = ffmpeg.probe(file_path)
        duration += float(probe["format"]["duration"])

    return duration


# Pegar o tempo dos comentarios
def get_total_non_postaudio_duration(reddit_id):
    audio_files = [
        f for f in os.listdir(f"assets/temp/{reddit_id}/mp3")
        if f.endswith(".mp3")
        and "postaudio" not in f
        and "title" not in f
        and "silence" not in f
    ]
    total_duration = 0.0
    for file_name in audio_files:
        duration = float(
            ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/{file_name}")["format"][
                "duration"
            ]
        )
        total_duration += duration
    return total_duration


def get_duration_postaudio(reddit_id):
    folder_path = f"assets/temp/{reddit_id}/mp3"
    filenames = [f for f in os.listdir(
        folder_path) if "postaudio" in f and f.endswith(".mp3")]
    filenames.sort(key=lambda f: int(f[len("postaudio-"):-len(".mp3")]))

    duration = []
    for filename in filenames:
        file_path = os.path.join(folder_path, filename)
        duration.append(float(ffmpeg.probe(file_path)["format"]["duration"]))

    return duration


def get_duration_non_postaudio(reddit_id):
    folder_path = f"assets/temp/{reddit_id}/mp3"
    filenames = [f for f in os.listdir(
        folder_path) if f.endswith(".mp3") and "postaudio" not in f and "title" not in f and "silence" not in f]
    filenames.sort(key=lambda f: int(''.join(filter(str.isdigit, f))))

    duration = []
    for filename in filenames:
        file_path = os.path.join(folder_path, filename)
        duration.append(float(ffmpeg.probe(file_path)["format"]["duration"]))

    return duration


# Aplicar transparência nas imagens


def apply_transparency(reddit_id, transparency):
    # 1. Acessa a pasta e lista os nomes de arquivos com 'comment_' e terminados em .png
    files = os.listdir(f"assets/temp/{reddit_id}/png/")
    files = [f for f in files if f.endswith(".png") and "comment_" in f]

    # 2. Transforma as imagens aplicando a transparência definida
    for file in files:
        img = Image.open(f"assets/temp/{reddit_id}/png/{file}").convert("RGBA")
        alpha = int(255 * transparency)
        datas = img.getdata()
        newData = []
        for item in datas:
            newData.append((item[0], item[1], item[2], alpha))
        img.putdata(newData)
        img.save(f"assets/temp/{reddit_id}/png/{file}")


def make_final_video(
    number_of_clips: int,
    length: int,
    reddit_obj: dict,
    background_config: Tuple[str, str, str, Any],
):
    """Gathers audio clips, gathers all screenshots, stitches them together and saves the final video to assets/temp
    Args:
        number_of_clips (int): Index to end at when going through the screenshots'
        length (int): Length of the video
        reddit_obj (dict): The reddit object that contains the posts to read.
        background_config (Tuple[str, str, str, Any]): The background config to use.
    """
    # settings values
    W: Final[int] = int(settings.config["settings"]["resolution_w"])
    H: Final[int] = int(settings.config["settings"]["resolution_h"])

    reddit_id = re.sub(r"[^\w\s-]", "", reddit_obj["thread_id"])
    print_step("Creating the final video 🎥")

    background_clip = ffmpeg.input(prepare_background(reddit_id, W=W, H=H))

    # Gather all audio clips
    audio_clips = list()
    if settings.config["settings"]["storymode"]:
        if settings.config["settings"]["storymodemethod"] == 0:
            audio_clips = [ffmpeg.input(
                f"assets/temp/{reddit_id}/mp3/title.mp3")]
            audio_clips.insert(
                1, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio.mp3")
            )
        elif settings.config["settings"]["storymodemethod"] == 1:
            audio_clips = [
                ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio-{i}.mp3")
                for i in track(
                    range(number_of_clips + 1), "Collecting the audio files..."
                )
            ]
            audio_clips.insert(
                0, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3")
            )
        elif settings.config["settings"]["storymodemethod"] == 2:
            # # Ele adiciona primeiro os áudios do post principal
            # audio_clips = [
            #     ffmpeg.input(f"assets/temp/{reddit_id}/mp3/postaudio-{i}.mp3")
            #     for i in track(
            #         range(number_of_clips + 1), "Collecting the audio files..."
            #     )
            # ]
            # # Depois adiciona os áudios dos comentários
            # comment_clips = [
            #     ffmpeg.input(f"assets/temp/{reddit_id}/mp3/{i}.mp3")
            #     for i in range(number_of_clips)
            # ]
            # # Por fim adiciona o áudio do título
            # audio_clips.insert(
            #     0, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3")
            # )

            # audio_clips.extend(comment_clips)

            # print(audio_clips)

            # print('\n\n')

            # print(get_postaudio_inputs(reddit_id))

            # print('\n\n')

            # print(get_non_postaudio_inputs(reddit_id))

            # print('\n\n')

            audio_clips = []
            audio_clips.extend(get_postaudio_inputs(reddit_id))
            audio_clips.extend(get_non_postaudio_inputs(reddit_id))
            audio_clips.insert(
                0, ffmpeg.input(f"assets/temp/{reddit_id}/mp3/title.mp3")
            )

            # Agora fazemos a mesma sequência para calcular o tempo dos áudios

            # Post principal
            audio_clips_durations = get_duration_postaudio(reddit_id)
            # Comentários
            audio_clips_durations.extend(
                get_duration_non_postaudio(reddit_id))
            # Título
            audio_clips_durations.insert(
                0,
                float(
                    ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"][
                        "duration"
                    ]
                ),
            )

            # print('\n\n')
            # print(audio_clips_durations)
            # print(type(audio_clips_durations))
            # print('\n\n')
            # input()

            if float(settings.config["settings"]["opacity"]) < 1.0:
                apply_transparency(reddit_id, float(
                    settings.config["settings"]["opacity"]))

    else:
        audio_clips = [
            ffmpeg.input(f"assets/temp/{reddit_id}/mp3/{i}.mp3")
            for i in range(number_of_clips)
        ]
        audio_clips.insert(0, ffmpeg.input(
            f"assets/temp/{reddit_id}/mp3/title.mp3"))

        audio_clips_durations = [
            float(
                ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/{i}.mp3")["format"][
                    "duration"
                ]
            )
            for i in range(number_of_clips)
        ]
        audio_clips_durations.insert(
            0,
            float(
                ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"][
                    "duration"
                ]
            ),
        )
    audio_concat = ffmpeg.concat(*audio_clips, a=1, v=0)
    ffmpeg.output(
        audio_concat, f"assets/temp/{reddit_id}/audio.mp3", **{"b:a": "192k"}
    ).overwrite_output().run(quiet=True)

    console.log(f"[bold green] Video Will Be: {length} Seconds Long")

    screenshot_width = int((W * 35) // 100)
    audio = ffmpeg.input(f"assets/temp/{reddit_id}/audio.mp3")

    image_clips = list()

    image_clips.insert(
        0,
        ffmpeg.input(f"assets/temp/{reddit_id}/png/title.png")["v"].filter(
            "scale", screenshot_width, -1
        ),
    )

    current_time = 0
    if settings.config["settings"]["storymode"]:
        if settings.config["settings"]["storymodemethod"] != 2:
            audio_clips_durations = [
                float(
                    ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/postaudio-{i}.mp3")[
                        "format"
                    ]["duration"]
                )
                for i in range(number_of_clips)
            ]
            audio_clips_durations.insert(
                0,
                float(
                    ffmpeg.probe(f"assets/temp/{reddit_id}/mp3/title.mp3")["format"][
                        "duration"
                    ]
                ),
            )
            # print('\n\n')
            # print(audio_clips_durations)
            # print('\n\n')
            # input()
        if settings.config["settings"]["storymodemethod"] == 0:
            image_clips.insert(
                1,
                ffmpeg.input(f"assets/temp/{reddit_id}/png/story_content.png").filter(
                    "scale", screenshot_width, -1
                ),
            )
            background_clip = background_clip.overlay(
                image_clips[1],
                enable=f"between(t,{current_time},{current_time + audio_clips_durations[1]})",
                x="(main_w-overlay_w)/2",
                y="(main_h-overlay_h)/2",
            )
            current_time += audio_clips_durations[1]
        elif settings.config["settings"]["storymodemethod"] == 1:
            for i in track(
                range(0, number_of_clips + 1), "Collecting the image files..."
            ):
                image_clips.append(
                    ffmpeg.input(f"assets/temp/{reddit_id}/png/img{i}.png")["v"].filter(
                        "scale", screenshot_width, -1
                    )
                )
                background_clip = background_clip.overlay(
                    image_clips[i],
                    enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                    x="(main_w-overlay_w)/2",
                    y="(main_h-overlay_h)/2",
                )
                current_time += audio_clips_durations[i]
        elif settings.config["settings"]["storymodemethod"] == 2:
            for i in track(
                range(0, len(get_duration_postaudio(reddit_id)) +
                      1), "Collecting the image files..."
            ):
                image_clips.append(
                    ffmpeg.input(f"assets/temp/{reddit_id}/png/img{i}.png")["v"].filter(
                        "scale", screenshot_width, -1
                    )
                )
                background_clip = background_clip.overlay(
                    image_clips[i],
                    enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                    x="(main_w-overlay_w)/2",
                    y="(main_h-overlay_h)/2",
                )
                current_time += audio_clips_durations[i]

            # Agora mostrar os comentários
            listaSoComentarios = audio_clips_durations[len(
                get_duration_postaudio(reddit_id)) + 1:]
            for i, elem in enumerate(listaSoComentarios):
                image_clips.append(
                    ffmpeg.input(f"assets/temp/{reddit_id}/png/comment_{i}.png")[
                        "v"
                    ].filter("scale", screenshot_width, -1)
                )
                background_clip = background_clip.overlay(
                    image_clips[-1],
                    enable=f"between(t,{current_time},{current_time + listaSoComentarios[i]})",
                    x="(main_w-overlay_w)/2",
                    y="(main_h-overlay_h)/2",
                )
                current_time += listaSoComentarios[i]
    else:
        for i in range(0, number_of_clips + 1):
            image_clips.append(
                ffmpeg.input(f"assets/temp/{reddit_id}/png/comment_{i}.png")[
                    "v"
                ].filter("scale", screenshot_width, -1)
            )
            background_clip = background_clip.overlay(
                image_clips[i],
                enable=f"between(t,{current_time},{current_time + audio_clips_durations[i]})",
                x="(main_w-overlay_w)/2",
                y="(main_h-overlay_h)/2",
            )
            current_time += audio_clips_durations[i]

    title = re.sub(r"[^\w\s-]", "", reddit_obj["thread_title"])
    idx = re.sub(r"[^\w\s-]", "", reddit_obj["thread_id"])
    title_thumb = reddit_obj["thread_title"]

    filename = f"{name_normalize(title)[:251]}"
    subreddit = settings.config["reddit"]["thread"]["subreddit"]

    if not exists(f"./results/{subreddit}"):
        print_substep("The results folder didn't exist so I made it")
        os.makedirs(f"./results/{subreddit}")

    # create a thumbnail for the video
    settingsbackground = settings.config["settings"]["background"]

    if settingsbackground["background_thumbnail"]:
        if not exists(f"./results/{subreddit}/thumbnails"):
            print_substep(
                "The results/thumbnails folder didn't exist so I made it")
            os.makedirs(f"./results/{subreddit}/thumbnails")
        # get the first file with the .png extension from assets/backgrounds and use it as a background for the thumbnail
        first_image = next(
            (
                file
                for file in os.listdir("assets/backgrounds")
                if file.endswith(".png")
            ),
            None,
        )
        if first_image is None:
            print_substep("No png files found in assets/backgrounds", "red")

        else:
            font_family = settingsbackground["background_thumbnail_font_family"]
            font_size = settingsbackground["background_thumbnail_font_size"]
            font_color = settingsbackground["background_thumbnail_font_color"]
            thumbnail = Image.open(f"assets/backgrounds/{first_image}")
            width, height = thumbnail.size
            thumbnailSave = create_thumbnail(
                thumbnail,
                font_family,
                font_size,
                font_color,
                width,
                height,
                title_thumb,
            )
            thumbnailSave.save(f"./assets/temp/{reddit_id}/thumbnail.png")
            print_substep(
                f"Thumbnail - Building Thumbnail in assets/temp/{reddit_id}/thumbnail.png"
            )

    text = f"@ocaradashistoriasreddit"
    background_clip = ffmpeg.drawtext(
        background_clip,
        text=text,
        x=f"(w-text_w)",
        y=f"(h-text_h)",
        fontsize=12,
        fontcolor="White",
        fontfile=os.path.join("fonts", "Roboto-Regular.ttf"),
    )
    print_step("Rendering the video 🎥")
    from tqdm import tqdm

    pbar = tqdm(total=100, desc="Progress: ",
                bar_format="{l_bar}{bar}", unit=" %")

    def on_update_example(progress):
        status = round(progress * 100, 2)
        old_percentage = pbar.n
        pbar.update(status - old_percentage)

    # def compress_video(path: str, max_size_mb: int) -> None:
    #     """Compresses a video file to a maximum size in MB and replaces the original file.

    #     Args:
    #         path (str): Path of the video file to be compressed.
    #         max_size_mb (int): Maximum size in MB for the compressed video file.
    #     """
    #     video = VideoFileClip(path)
    #     video_duration = video.duration
    #     video_size = os.path.getsize(path) / 1000000  # convert to MB
    #     if video_size <= max_size_mb:
    #         print(f"The video size is already lower than {max_size_mb} MB.")
    #         return
    #     else:
    #         bitrate = ceil((video_size - max_size_mb) / video_duration)
    #         compressed_video_path = f"{os.path.splitext(path)[0]}_compressed.mp4"

    #         ffmpeg_cmd = (
    #             f'ffmpeg -i "{path}" -c:v libx264 -b:v {bitrate}M -maxrate:v {bitrate}M '
    #             f'-bufsize:v {2*bitrate}M -c:a copy "{compressed_video_path}"'
    #         )

    #         os.system(ffmpeg_cmd)
    #         compressed_video_size = os.path.getsize(
    #             compressed_video_path) / 1000000  # convert to MB

    #         if compressed_video_size <= max_size_mb:
    #             os.replace(compressed_video_path, path)
    #             print(
    #                 f"The video has been successfully compressed to {compressed_video_size:.2f} MB and replaced the original file.")
    #         else:
    #             print("An error occurred during the compression process. The compressed video is larger than the specified maximum size.")
    #         return

    path = f"results/{subreddit}/{filename}"
    path = path[:251]
    path = path + ".mp4"

    with ProgressFfmpeg(length, on_update_example) as progress:
        ffmpeg.output(
            background_clip,
            audio,
            path,
            f="mp4",
            **{
                "c:v": "h264",
                "b:v": "20M",
                "b:a": "192k",
                "threads": multiprocessing.cpu_count(),
            },
        ).overwrite_output().global_args("-progress", progress.output_file.name).run(
            quiet=True,
            overwrite_output=True,
            capture_stdout=False,
            capture_stderr=False,
        )

    old_percentage = pbar.n
    pbar.update(100 - old_percentage)
    pbar.close()

    print_step("Diminuindo tamanho do arquivo... ♻️")

    def compress_video(path: str, max_size_mb: int) -> None:
        """Compresses a video file to a maximum size in MB and replaces the original file.

        Args:
            path (str): Path of the video file to be compressed.
            max_size_mb (int): Maximum size in MB for the compressed video file.
        """
        video = VideoFileClip(path)
        video_duration = video.duration
        video_size = os.path.getsize(path) / 1000000  # convert to MB
        if video_size <= max_size_mb:
            print(f"The video size is already lower than {max_size_mb} MB.")
            return
        else:
            bitrate = ceil((video_size - max_size_mb) / video_duration)
            compressed_video_path = f"{os.path.splitext(path)[0]}_compressed.mp4"

            ffmpeg_cmd = (
                f'ffmpeg -i "{path}" -c:v libx264 -b:v {bitrate}M -maxrate:v {bitrate}M '
                f'-bufsize:v {2*bitrate}M -c:a copy "{compressed_video_path}"'
            )

            os.system(ffmpeg_cmd)
            compressed_video_size = os.path.getsize(
                compressed_video_path) / 1000000  # convert to MB

            if compressed_video_size <= max_size_mb:
                os.replace(compressed_video_path, path)
                print(
                    f"The video has been successfully compressed to {compressed_video_size:.2f} MB and replaced the original file.")
            else:
                print("An error occurred during the compression process. The compressed video is larger than the specified maximum size.")
            return

    compress_video(path, 20)

    save_data(subreddit, filename + ".mp4", title, idx, background_config[2])
    print_step("Removing temporary files 🗑")
    cleanup(reddit_id)
    print_substep(f"Removed {reddit_id} temporary files 🗑")
    print_step("Done! 🎉 The video is in the results folder 📁")

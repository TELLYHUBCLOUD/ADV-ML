import os
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from os import path as ospath, walk
from time import time

from aiofiles.os import path as aiopath, remove
from aiofiles.os import rename as aiorename
from aioshutil import move
import aiohttp
from aiofiles import open as aiopen

from ... import LOGGER, DOWNLOAD_DIR, cpu_eater_lock, task_dict, task_dict_lock
from ...core.config_manager import BinConfig
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.files_utils import get_path_size
from ..ext_utils.media_utils import FFMpeg, get_media_info
from ..mirror_leech_utils.status_utils.metadata_status import MetadataStatus


def is_mkv(file_path):
    """Check if file is an MKV file"""
    return file_path.lower().endswith(".mkv")


async def download_attachment(url):
    """Download attachment image from URL and save to temp directory"""
    try:
        temp_dir = f"{DOWNLOAD_DIR}attachments"
        os.makedirs(temp_dir, exist_ok=True)

        # Get file extension from URL
        ext = ospath.splitext(url.split("?")[0])[1] or ".jpg"
        if ext.lower() not in [".jpg", ".jpeg", ".png"]:
            ext = ".jpg"

        output_path = ospath.join(temp_dir, f"attachment_{int(time())}{ext}")

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    async with aiopen(output_path, "wb") as f:
                        await f.write(await response.read())
                    LOGGER.info(f"Downloaded attachment from {url}")
                    return output_path
                else:
                    LOGGER.error(
                        f"Failed to download attachment. Status: {response.status}"
                    )
                    return None
    except Exception as e:
        LOGGER.error(f"Error downloading attachment: {e}")
        return None


async def attach_to_mkv(file_path, attachment_path):
    """
    Attach an image file to MKV container using FFmpeg

    Args:
        file_path: Path to the MKV file
        attachment_path: Path to the image to attach

    Returns:
        temp_file path if successful, None otherwise
    """
    try:
        temp_file = f"{file_path}.temp.mkv"

        # Determine MIME type and filename based on extension
        attachment_ext = attachment_path.split(".")[-1].lower()
        mime_type = "application/octet-stream"
        if attachment_ext in ["jpg", "jpeg"]:
            mime_type = "image/jpeg"
        elif attachment_ext == "png":
            mime_type = "image/png"

        attachment_name = "cover"  # Default name for the attachment

        # Build FFmpeg command to attach the image
        cmd = [
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            file_path,
            "-attach",
            attachment_path,
            "-metadata:s:t",
            f"mimetype={mime_type}",
            "-metadata:s:t",
            f"filename={attachment_name}.{attachment_ext}",
            "-disposition:t",
            "default",
            "-c",
            "copy",
            "-map",
            "0",
            "-map",
            "0:t?",
            "-threads",
            str(max(1, (os.cpu_count() or 2) // 2)),
            temp_file,
        ]

        return cmd, temp_file
    except Exception as e:
        LOGGER.error(f"Error creating attachment command: {e}")
        return None, None


async def apply_attachment(self, dl_path, gid, attachment_url):
    """
    Apply attachment to all MKV files in the given path

    Args:
        self: TaskListener instance
        dl_path: Path to downloaded content
        gid: Task GID
        attachment_url: URL of the attachment image

    Returns:
        dl_path after processing
    """
    if not attachment_url:
        return dl_path

    LOGGER.info(f"Applying attachment from {attachment_url} to {self.name}")

    # Download the attachment image
    attachment_path = await download_attachment(attachment_url)
    if not attachment_path:
        LOGGER.error("Failed to download attachment, skipping...")
        return dl_path

    try:
        ffmpeg = FFMpeg(self)
        is_file = await aiopath.isfile(dl_path)

        # Collect all MKV files
        mkv_files = []
        if is_file:
            if is_mkv(dl_path):
                mkv_files.append(dl_path)
        else:
            for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
                for file in files:
                    file_path = ospath.join(dirpath, file)
                    if is_mkv(file_path):
                        mkv_files.append(file_path)

        if not mkv_files:
            LOGGER.info(f"No MKV files found in {dl_path} to attach image.")
            return dl_path

        # Process each MKV file
        async with task_dict_lock:
            task_dict[self.mid] = MetadataStatus(self, ffmpeg, gid, "attachment")
        self.progress = False
        await cpu_eater_lock.acquire()
        self.progress = True

        try:
            for file_path in mkv_files:
                if self.is_cancelled:
                    break

                self.subname = ospath.basename(file_path)
                self.subsize = await get_path_size(file_path)

                # Get attachment command
                cmd, temp_file = await attach_to_mkv(file_path, attachment_path)
                if not cmd:
                    LOGGER.error(f"Failed to create command for {file_path}")
                    continue

                # Clear FFmpeg state and set total time
                ffmpeg.clear()
                media_info = await get_media_info(file_path)
                if media_info:
                    ffmpeg._total_time = media_info[0]

                LOGGER.info(f"Attaching image to: {file_path}")

                # Execute FFmpeg command
                self.subproc = await create_subprocess_exec(
                    *cmd, stdout=PIPE, stderr=PIPE
                )
                await ffmpeg._ffmpeg_progress()
                _, stderr = await self.subproc.communicate()
                stderr_text = stderr.decode().strip() if stderr else ""

                if self.is_cancelled:
                    if await aiopath.exists(temp_file):
                        await remove(temp_file)
                    break

                if self.subproc.returncode == 0:
                    LOGGER.info(f"Successfully attached image to {file_path}")
                    await remove(file_path)
                    await aiorename(temp_file, file_path)
                else:
                    LOGGER.error(f"Error attaching image to {file_path}: {stderr_text}")
                    if await aiopath.exists(temp_file):
                        await remove(temp_file)
        finally:
            cpu_eater_lock.release()

    except Exception as e:
        LOGGER.error(f"Error in apply_attachment: {e}")
    finally:
        # Always clean up downloaded attachment
        if await aiopath.exists(attachment_path):
            await remove(attachment_path)
            LOGGER.info(f"Cleaned up attachment file: {attachment_path}")

    return dl_path

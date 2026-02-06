from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from asyncio import create_subprocess_exec, sleep, gather
from natsort import natsorted
from os import path as ospath, walk
from time import time

from bot import bot_loop, task_dict, task_dict_lock, LOGGER
from bot.core.config_manager import BinConfig
from bot.helper.ext_utils.bot_utils import sync_to_async
from bot.helper.ext_utils.files_utils import get_path_size, clean_target
from bot.helper.ext_utils.media_utils import get_document_type
from bot.helper.mirror_leech_utils.status_utils.merge_status import MergeStatus
from bot.helper.telegram_helper.message_utils import update_status_message


class Merge:
    def __init__(self, listener):
        self._listener = listener
        self._processed_bytes = 0
        self._start_time = time()

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except ZeroDivisionError:
            return 0

    async def _progress(self, outfile):
        while True:
            await sleep(1)
            if await aiopath.exists(outfile):
                self._processed_bytes = await get_path_size(outfile)

    async def merge_vids(self, path, gid, keep_original=False, custom_name=None):
        list_files, remove_files, size = [], [], 0
        original_dir = None  # Track the directory where original files are located
        
        for dirpath, _, files in await sync_to_async(walk, path):
            for file in natsorted(files):
                video_file = ospath.join(dirpath, file)
                doc_type = await get_document_type(video_file)
                if doc_type[0]:
                    size += await get_path_size(video_file)
                    list_files.append(f"file '{video_file}'")
                    remove_files.append(video_file)
                    # Store the directory of the first video file found
                    if original_dir is None:
                        original_dir = dirpath
        
        LOGGER.info(f'Merge check for: {path} | Found {len(list_files)} video file(s)')
        
        if len(list_files) > 1:
            # Use custom name if provided, otherwise use directory name
            if custom_name:
                name_without_ext = ospath.splitext(custom_name)[0]
                LOGGER.info(f'Using custom merge name: {name_without_ext}')
            else:
                name = ospath.basename(path)
                name_without_ext = ospath.splitext(name)[0]
            async with task_dict_lock:
                task_dict[self._listener.mid] = MergeStatus(name_without_ext, size, gid, self, self._listener)
            await update_status_message(self._listener.message.chat.id)
            
            input_file = ospath.join(path, 'input.txt')
            async with aiopen(input_file, 'w') as f:
                await f.write('\n'.join(list_files))
            
            LOGGER.info(f'Merging {len(list_files)} videos --> {name_without_ext}.mkv')
            
            # If keeping originals, create merged file in the same directory as originals
            # Otherwise create in root path
            if keep_original and original_dir and original_dir != path:
                outfile = ospath.join(original_dir, f'{name_without_ext}.mkv')
            else:
                outfile = ospath.join(path, f'{name_without_ext}.mkv')
            
            cmd = [BinConfig.FFMPEG_NAME, '-ignore_unknown', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', input_file, '-map', '0', '-c', 'copy', outfile]
            
            self._listener.subproc = await create_subprocess_exec(*cmd)
            task = bot_loop.create_task(self._progress(outfile))
            code = await self._listener.subproc.wait()
            task.cancel()
            
            if self._listener.subproc == 'cancelled' or code == -9:
                return False
            elif code == 0:
                await clean_target(input_file)
                if not self._listener.seed and not keep_original:
                    await gather(*[clean_target(file) for file in remove_files])
                LOGGER.info(f'Merge successfully with name: {name_without_ext}.mkv')
                # If keeping originals, return the subdirectory where merged file was created
                # Otherwise return the single merged file path
                return original_dir if (keep_original and original_dir and original_dir != path) else outfile
            else:
                LOGGER.error(f'Failed to merge: {name_without_ext}.mkv')
                return False
        else:
            # Nothing to merge, just return original path
            return path

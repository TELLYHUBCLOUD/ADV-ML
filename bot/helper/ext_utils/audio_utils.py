import os
import json
from asyncio import create_subprocess_exec, Event, wait_for, TimeoutError, sleep
from asyncio.subprocess import PIPE
from os import path as ospath, walk
from time import time

from aiofiles.os import path as aiopath, remove
from aiofiles.os import rename as aiorename
from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler

from ... import LOGGER, cpu_eater_lock, task_dict, task_dict_lock, threads, cores
from ...core.config_manager import BinConfig
from ...core.tg_client import TgClient
from ..ext_utils.bot_utils import sync_to_async, new_task
from ..ext_utils.files_utils import get_path_size
from ..ext_utils.media_utils import FFMpeg, get_media_info
from ..ext_utils.status_utils import get_readable_file_size, EngineStatus, MirrorStatus
from ..mirror_leech_utils.status_utils.metadata_status import MetadataStatus
from ..telegram_helper.button_build import ButtonMaker
from ..telegram_helper.message_utils import send_message, edit_message, delete_message, update_status_message


# Audio Selection Status Class
class AudioSelectionStatus:
    def __init__(self, listener, gid):
        self.listener = listener
        self._gid = gid
        self._size = self.listener.size
        self.engine = EngineStatus().STATUS_AUDIO_SELECT

    def gid(self):
        return self._gid

    def name(self):
        return self.listener.name

    def size(self):
        return get_readable_file_size(self._size)

    def status(self):
        return MirrorStatus.STATUS_AUDIO_SELECT

    def task(self):
        return self

    def progress(self):
        return "0%"

    def processed_bytes(self):
        return 0

    def speed(self):
        return "0B/s"

    def eta(self):
        return "-"

    async def cancel_task(self):
        LOGGER.info(f"Cancelling Audio Selection: {self.listener.name}")
        self.listener.is_cancelled = True


# Global dictionary to store audio selection sessions
audio_selection_sessions = {}


def is_mkv(file_path):
    """Check if file is an MKV file"""
    return file_path.lower().endswith('.mkv')


async def get_audio_stream_count(file_path):
    """Get the number of audio streams in a file"""
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'a', 
        '-show_entries', 'stream=index', '-of', 'csv=p=0', file_path
    ]
    process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        err = stderr.decode().strip()
        LOGGER.error(f"FFprobe error: {err}")
        return 0
    
    audio_streams = stdout.decode().strip().split('\n')
    return len([s for s in audio_streams if s])


async def get_audio_tracks_info(file_path):
    """Get detailed information about all audio tracks in a file"""
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'a',
        '-show_entries', 'stream=index:stream_tags=language,title',
        '-of', 'json', file_path
    ]
    process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        err = stderr.decode().strip()
        LOGGER.error(f"FFprobe error getting audio info: {err}")
        return []
    
    try:
        data = json.loads(stdout.decode())
        streams = data.get('streams', [])
        audio_tracks = []
        
        for idx, stream in enumerate(streams):
            tags = stream.get('tags', {})
            language = tags.get('language', 'Unknown')
            title = tags.get('title', '')
            
            # Create display name
            if title:
                display_name = f"{language.upper()} - {title}"
            else:
                display_name = language.upper()
            
            audio_tracks.append({
                'index': idx,
                'language': language,
                'title': title,
                'display_name': display_name
            })
        
        return audio_tracks
    except Exception as e:
        LOGGER.error(f"Error parsing audio track info: {e}")
        return []


async def remove_audio_tracks(file_path, audio_indices):
    """
    Remove specific audio tracks from MKV file
    
    Args:
        file_path: Path to the MKV file
        audio_indices: Comma-separated string of audio track indices to remove (e.g., "0,2,3")
    
    Returns:
        tuple: (ffmpeg_command, temp_file_path) or (None, None) if validation fails
    """
    try:
        # Parse audio indices
        audio_keys = [idx.strip() for idx in audio_indices.split(',')]
        
        # Validate indices are integers
        try:
            audio_keys_int = [int(key) for key in audio_keys]
        except ValueError:
            LOGGER.error(f"Invalid audio indices: {audio_indices}. All indices must be integers.")
            return None, None
        
        # Get total audio stream count
        audio_stream_count = await get_audio_stream_count(file_path)
        
        if audio_stream_count == 0:
            LOGGER.warning(f"No audio streams found in {file_path}")
            return None, None
        
        # Validate indices are within range
        if any(key < 0 or key >= audio_stream_count for key in audio_keys_int):
            LOGGER.error(f"Audio indices {audio_indices} out of range. File has {audio_stream_count} audio streams (0-{audio_stream_count-1}).")
            return None, None
        
        # Check if all audio tracks are being removed
        if len(audio_keys_int) >= audio_stream_count:
            LOGGER.warning(f"Attempting to remove all {audio_stream_count} audio tracks from {file_path}. Video will have no audio.")
        
        temp_file = f"{file_path}.temp.mkv"
        
        # Build FFmpeg command
        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            file_path,
            "-map",
            "0",  # Map everything from input
        ]
        
        # Remove specific audio tracks
        for audio_key in audio_keys:
            cmd.extend(["-map", f"-0:a:{audio_key}"])
        
        # Copy codecs and set threads
        cmd.extend([
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            "-threads",
            f"{threads}",
            temp_file,
        ])
        
        return cmd, temp_file
        
    except Exception as e:
        LOGGER.error(f"Error creating audio removal command: {e}")
        return None, None


async def change_audio_order(file_path, audio_indices):
    """
    Change audio track order in MKV file
    
    Args:
        file_path: Path to the MKV file
        audio_indices: Comma-separated string of audio track order (e.g., "2,3,0,1")
    
    Returns:
        tuple: (ffmpeg_command, temp_file_path) or (None, None) if validation fails
    """
    try:
        # Parse audio indices
        audio_keys = [idx.strip() for idx in audio_indices.split(',')]
        
        # Validate indices are integers
        try:
            audio_keys_int = [int(key) for key in audio_keys]
        except ValueError:
            LOGGER.error(f"Invalid audio indices: {audio_indices}. All indices must be integers.")
            return None, None
        
        # Get total audio stream count
        audio_stream_count = await get_audio_stream_count(file_path)
        
        if audio_stream_count == 0:
            LOGGER.warning(f"No audio streams found in {file_path}")
            return None, None
        
        # Validate: More indices than available streams
        if len(audio_keys_int) > audio_stream_count:
            LOGGER.error(f"Invalid key format: {audio_indices}. More indices ({len(audio_keys_int)}) provided than available audio streams ({audio_stream_count}).")
            return None, None
        
        # Validate indices are within range
        if any(key < 0 or key >= audio_stream_count for key in audio_keys_int):
            LOGGER.error(f"Audio indices {audio_indices} out of range. File has {audio_stream_count} audio streams (0-{audio_stream_count-1}).")
            return None, None
        
        temp_file = f"{file_path}.temp.mkv"
        
        # Build FFmpeg command
        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            file_path,
            "-map",
            "0:v:0",  # Map video
        ]
        
        # Map audio in the specified order and set dispositions
        for index, audio_key in enumerate(audio_keys_int):
            cmd.extend(["-map", f"0:a:{audio_key}"])
            if index == 0:
                cmd.extend(["-disposition:a:0", "default"])
            else:
                cmd.extend([f"-disposition:a:{index}", "none"])
        
        # Copy codecs and set threads
        cmd.extend([
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-map",
            "0:s?",
            "-c:s",
            "copy",
            "-threads",
            f"{threads}",
            temp_file,
        ])
        
        return cmd, temp_file
        
    except Exception as e:
        LOGGER.error(f"Error creating audio order change command: {e}")
        return None, None


async def apply_audio_removal_interactive(self, dl_path, gid):
    """
    Interactive audio removal - shows UI for user to select tracks to remove
    
    Args:
        self: TaskListener instance
        dl_path: Download path
        gid: Task GID
    """
    try:
        # Get the first MKV file to analyze
        target_file = None
        if self.is_file and is_mkv(dl_path):
            target_file = dl_path
        else:
            # Find first MKV in directory
            for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
                for file_ in files:
                    file_path = ospath.join(dirpath, file_)
                    if is_mkv(file_path):
                        target_file = file_path
                        break
                if target_file:
                    break
        
        if not target_file:
            await send_message(self.message, f"{self.tag} No MKV files found for audio removal!")
            return dl_path
        
        # Get audio tracks info
        audio_tracks = await get_audio_tracks_info(target_file)
        
        if not audio_tracks:
            await send_message(self.message, f"{self.tag} No audio tracks found in the file!")
            return dl_path
        
        if len(audio_tracks) == 1:
            await send_message(self.message, f"{self.tag} File has only one audio track. Cannot remove all audio!")
            return dl_path
        
        # Create session
        session_id = f"{self.message.chat.id}_{self.mid}"
        audio_selection_sessions[session_id] = {
            'listener': self,
            'dl_path': dl_path,
            'gid': gid,
            'audio_tracks': audio_tracks,
            'selected_indices': set(),
            'new_name': None,
            'event': Event(),
            'start_time': time(),
            'timeout': 180,  # 3 minutes
            'awaiting_rename': False
        }
        
        # Set status to Audio Selection
        async with task_dict_lock:
            task_dict[self.mid] = AudioSelectionStatus(self, gid)
        
        # Send selection UI
        await send_audio_selection_message(session_id, self.message, self.tag)
        
        # Update status message after setting status
        await update_status_message(self.message.chat.id)
        
        # Wait for user selection with timeout
        try:
            await wait_for(audio_selection_sessions[session_id]['event'].wait(), timeout=180)
        except TimeoutError:
            await send_message(self.message, f"{self.tag} Audio removal timed out!")
            if session_id in audio_selection_sessions:
                del audio_selection_sessions[session_id]
            return dl_path
        
        # Check if cancelled
        if self.is_cancelled or session_id not in audio_selection_sessions:
            return dl_path
        
        # Get selected indices
        session_data = audio_selection_sessions[session_id]
        selected_indices = session_data['selected_indices']
        new_name = session_data['new_name']
        
        # Clean up session
        del audio_selection_sessions[session_id]
        
        if not selected_indices:
            await send_message(self.message, f"{self.tag} No audio tracks selected for removal. Proceeding without changes.")
            return dl_path
        
        # Check if trying to remove all tracks
        if len(selected_indices) >= len(audio_tracks):
            await send_message(self.message, f"{self.tag} Cannot remove all audio tracks! Proceeding without changes.")
            return dl_path
        
        # Convert to comma-separated string
        audio_indices = ','.join(map(str, sorted(selected_indices)))
        
        # Apply the actual removal
        result_path = await apply_audio_removal(self, dl_path, gid, audio_indices)
        
        # Apply rename if provided
        if new_name and result_path:
            result_path = await apply_rename(result_path, new_name, self.is_file)
        
        return result_path
        
    except Exception as e:
        LOGGER.error(f"Error in interactive audio removal: {e}")
        if session_id in audio_selection_sessions:
            del audio_selection_sessions[session_id]
        return dl_path


async def apply_rename(path, new_name, is_file):
    """Rename file or directory"""
    try:
        if is_file:
            # Rename file
            dir_path = ospath.dirname(path)
            ext = ospath.splitext(path)[1]
            new_path = ospath.join(dir_path, new_name + ext)
            await aiorename(path, new_path)
            return new_path
        else:
            # Rename directory
            parent_dir = ospath.dirname(path.rstrip('/'))
            new_path = ospath.join(parent_dir, new_name)
            await aiorename(path, new_path)
            return new_path
    except Exception as e:
        LOGGER.error(f"Error renaming: {e}")
        return path


async def send_audio_selection_message(session_id, message, tag):
    """Send the audio selection UI message"""
    session = audio_selection_sessions.get(session_id)
    if not session:
        return
    
    audio_tracks = session['audio_tracks']
    selected_indices = session['selected_indices']
    new_name = session['new_name']
    start_time = session['start_time']
    timeout = session['timeout']
    
    # Calculate remaining time
    elapsed = int(time() - start_time)
    remaining = max(0, timeout - elapsed)
    mins, secs = divmod(remaining, 60)
    
    # Build message with provided name and selected tracks list
    listener = session['listener']
    provided_name = getattr(listener, 'new_name', None) or ospath.basename(session['dl_path'])
    filename = new_name if new_name else provided_name
    
    # List of selected audio tracks
    selected_section = ""
    if selected_indices:
        selected_tracks = [track for track in audio_tracks if track['index'] in selected_indices]
        selected_section = "\n\nSelected tracks for REMOVE :\n"
        for track in selected_tracks:
            selected_section += f"├ {track['language'].upper()} (Track {track['index'] + 1})\n"
        selected_section = selected_section.rstrip('\n')
    
    msg_text = f"""<b><u><i>Audio Remove Settings</i></u> 
    
Req for: {tag}

Total Audio Tracks : {len(audio_tracks)}

Name : {filename}{selected_section}

Time Out : {mins}m{secs:02d}s</b>"""
    
    # Build buttons
    buttons = ButtonMaker()
    
    # Rename button at top
    buttons.data_button("📝 Rename", f"audr_{session_id}_rename", position="header")
    
    # Audio track selection buttons (2 per row) - Show only language code
    for track in audio_tracks:
        idx = track['index']
        lang = track['language'].upper()
        
        # Add checkmark if selected
        prefix = "✅ " if idx in selected_indices else ""
        button_text = f"{prefix}{lang}"
        
        buttons.data_button(button_text, f"audr_{session_id}_toggle_{idx}")
    
    # Done button at bottom
    buttons.data_button("✅ Done Selecting", f"audr_{session_id}_done", position="footer")
    
    # Send or update message
    if not session.get('ui_message'):
        sent_msg = await send_message(message, msg_text, buttons.build_menu(2))
        session['ui_message'] = sent_msg
    else:
        await edit_message(session['ui_message'], msg_text, buttons.build_menu(2))


async def apply_audio_removal(self, dl_path, gid, audio_indices):
    """
    Apply audio removal to MKV files
    
    Args:
        self: TaskListener instance
        dl_path: Download path
        gid: Task GID
        audio_indices: Comma-separated audio track indices to remove
    """
    if not audio_indices:
        LOGGER.info("No audio indices provided for removal")
        return dl_path
    
    ffmpeg = FFMpeg(self)
    checked = False
    attachment_path = None
    
    try:
        # Handle single file
        if self.is_file:
            if is_mkv(dl_path):
                cmd, temp_file = await remove_audio_tracks(dl_path, audio_indices)
                if cmd:
                    if not checked:
                        checked = True
                        async with task_dict_lock:
                            task_dict[self.mid] = MetadataStatus(self, ffmpeg, gid, "audio_remove")
                        self.progress = False
                        await cpu_eater_lock.acquire()
                        self.progress = True
                    
                    # Clear FFmpeg state and set total time
                    ffmpeg.clear()
                    media_info = await get_media_info(dl_path)
                    if media_info:
                        ffmpeg._total_time = media_info[0]
                    
                    LOGGER.info(f"Removing audio tracks {audio_indices} from: {dl_path}")
                    self.subsize = self.size
                    
                    # Execute FFmpeg command
                    self.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
                    await ffmpeg._ffmpeg_progress()
                    _, stderr = await self.subproc.communicate()
                    stderr_text = stderr.decode().strip() if stderr else ""
                    
                    if self.is_cancelled:
                        if await aiopath.exists(temp_file):
                            await remove(temp_file)
                    elif self.subproc.returncode == 0:
                        LOGGER.info(f"Successfully removed audio tracks from {dl_path}")
                        await remove(dl_path)
                        await aiorename(temp_file, dl_path)
                    else:
                        LOGGER.error(f"Error removing audio from {dl_path}: {stderr_text}")
                        if await aiopath.exists(temp_file):
                            await remove(temp_file)
        else:
            # Handle directory with multiple files
            for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
                for file_ in files:
                    file_path = ospath.join(dirpath, file_)
                    
                    if self.is_cancelled:
                        if checked:
                            cpu_eater_lock.release()
                        return dl_path
                    
                    self.proceed_count += 1
                    
                    if is_mkv(file_path):
                        cmd, temp_file = await remove_audio_tracks(file_path, audio_indices)
                        if cmd:
                            if not checked:
                                checked = True
                                async with task_dict_lock:
                                    task_dict[self.mid] = MetadataStatus(self, ffmpeg, gid, "audio_remove")
                                self.progress = False
                                await cpu_eater_lock.acquire()
                                self.progress = True
                            
                            # Clear FFmpeg state and set total time
                            ffmpeg.clear()
                            media_info = await get_media_info(file_path)
                            if media_info:
                                ffmpeg._total_time = media_info[0]
                            
                            LOGGER.info(f"Removing audio tracks {audio_indices} from: {file_path}")
                            self.subsize = await aiopath.getsize(file_path)
                            self.subname = file_
                            
                            # Execute FFmpeg command
                            self.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
                            await ffmpeg._ffmpeg_progress()
                            _, stderr = await self.subproc.communicate()
                            stderr_text = stderr.decode().strip() if stderr else ""
                            
                            if self.is_cancelled:
                                if await aiopath.exists(temp_file):
                                    await remove(temp_file)
                                break
                            elif self.subproc.returncode == 0:
                                LOGGER.info(f"Successfully removed audio tracks from {file_path}")
                                await remove(file_path)
                                await aiorename(temp_file, file_path)
                            else:
                                LOGGER.error(f"Error removing audio from {file_path}: {stderr_text}")
                                if await aiopath.exists(temp_file):
                                    await remove(temp_file)
        
        if checked:
            cpu_eater_lock.release()
        
        return dl_path
        
    except Exception as e:
        LOGGER.error(f"Error in apply_audio_removal: {e}")
        if checked:
            cpu_eater_lock.release()
        return dl_path


async def apply_audio_order_change(self, dl_path, gid, audio_indices):
    """
    Apply audio order change to MKV files
    
    Args:
        self: TaskListener instance
        dl_path: Download path
        gid: Task GID
        audio_indices: Comma-separated audio track order to apply
    """
    if not audio_indices:
        LOGGER.info("No audio indices provided for order change")
        return dl_path
    
    ffmpeg = FFMpeg(self)
    checked = False
    
    try:
        # Handle single file
        if self.is_file:
            if is_mkv(dl_path):
                cmd, temp_file = await change_audio_order(dl_path, audio_indices)
                if cmd:
                    if not checked:
                        checked = True
                        async with task_dict_lock:
                            task_dict[self.mid] = MetadataStatus(self, ffmpeg, gid, "audio_change")
                        self.progress = False
                        await cpu_eater_lock.acquire()
                        self.progress = True
                    
                    # Clear FFmpeg state and set total time
                    ffmpeg.clear()
                    media_info = await get_media_info(dl_path)
                    if media_info:
                        ffmpeg._total_time = media_info[0]
                    
                    LOGGER.info(f"Changing audio order {audio_indices} for: {dl_path}")
                    self.subsize = self.size
                    
                    # Execute FFmpeg command
                    self.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
                    await ffmpeg._ffmpeg_progress()
                    _, stderr = await self.subproc.communicate()
                    stderr_text = stderr.decode().strip() if stderr else ""
                    
                    if self.is_cancelled:
                        if await aiopath.exists(temp_file):
                            await remove(temp_file)
                    elif self.subproc.returncode == 0:
                        LOGGER.info(f"Successfully changed audio order for {dl_path}")
                        await remove(dl_path)
                        await aiorename(temp_file, dl_path)
                    else:
                        LOGGER.error(f"Error changing audio order for {dl_path}: {stderr_text}")
                        if await aiopath.exists(temp_file):
                            await remove(temp_file)
        else:
            # Handle directory with multiple files
            for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
                for file_ in files:
                    file_path = ospath.join(dirpath, file_)
                    
                    if self.is_cancelled:
                        if checked:
                            cpu_eater_lock.release()
                        return dl_path
                    
                    self.proceed_count += 1
                    
                    if is_mkv(file_path):
                        cmd, temp_file = await change_audio_order(file_path, audio_indices)
                        if cmd:
                            if not checked:
                                checked = True
                                async with task_dict_lock:
                                    task_dict[self.mid] = MetadataStatus(self, ffmpeg, gid, "audio_change")
                                self.progress = False
                                await cpu_eater_lock.acquire()
                                self.progress = True
                            
                            # Clear FFmpeg state and set total time
                            ffmpeg.clear()
                            media_info = await get_media_info(file_path)
                            if media_info:
                                ffmpeg._total_time = media_info[0]
                            
                            LOGGER.info(f"Changing audio order {audio_indices} for: {file_path}")
                            self.subsize = await aiopath.getsize(file_path)
                            self.subname = file_
                            
                            # Execute FFmpeg command
                            self.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
                            await ffmpeg._ffmpeg_progress()
                            _, stderr = await self.subproc.communicate()
                            stderr_text = stderr.decode().strip() if stderr else ""
                            
                            if self.is_cancelled:
                                if await aiopath.exists(temp_file):
                                    await remove(temp_file)
                                break
                            elif self.subproc.returncode == 0:
                                LOGGER.info(f"Successfully changed audio order for {file_path}")
                                await remove(file_path)
                                await aiorename(temp_file, file_path)
                            else:
                                LOGGER.error(f"Error changing audio order for {file_path}: {stderr_text}")
                                if await aiopath.exists(temp_file):
                                    await remove(temp_file)
        
        if checked:
            cpu_eater_lock.release()
        
        return dl_path
        
    except Exception as e:
        LOGGER.error(f"Error in apply_audio_order_change: {e}")
        if checked:
            cpu_eater_lock.release()
        return dl_path


@new_task
async def audio_selection_callback(_, query):
    """Handle audio selection button callbacks"""
    data = query.data
    message = query.message
    user_id = query.from_user.id
    
    try:
        # Parse callback data: audr_sessionid_action_value
        parts = data.split('_', 3)
        if len(parts) < 3:
            await query.answer("Invalid callback data", show_alert=True)
            return
        
        session_id = f"{parts[1]}_{parts[2]}"
        action = parts[3] if len(parts) > 3 else parts[2]
        
        # Get session
        session = audio_selection_sessions.get(session_id)
        if not session:
            await query.answer("Session expired!", show_alert=True)
            await delete_message(message)
            return
        
        # Check if user is authorized
        listener = session['listener']
        if user_id != listener.user_id:
            await query.answer("You are not authorized to use this!", show_alert=True)
            return
        
        # Check if awaiting rename input
        if session.get('awaiting_rename'):
            await query.answer("Please send the new filename first!", show_alert=True)
            return
        
        # Handle actions
        if action == 'rename':
            session['awaiting_rename'] = True
            await query.answer()
            rename_msg = await send_message(
                message,
                f"<b>{listener.tag} Send the new filename (without extension) within 60 seconds:</b>"
            )
            session['rename_msg'] = rename_msg
            
            # Start listening for rename input
            from ...core.tg_client import TgClient
            from pyrogram.handlers import MessageHandler
            from pyrogram.filters import text
            
            async def handle_rename_input(_, msg):
                if msg.from_user.id == user_id and msg.chat.id == message.chat.id:
                    # Remove handler
                    TgClient.bot.remove_handler(rename_handler)
                    
                    # Save new name
                    session['new_name'] = msg.text.strip()
                    session['awaiting_rename'] = False
                    
                    # Delete rename messages
                    await delete_message(session['rename_msg'])
                    await delete_message(msg)
                    
                    # Update UI
                    await send_audio_selection_message(session_id, message, listener.tag)
                    return
            
            rename_handler = MessageHandler(handle_rename_input, filters=text & user(user_id))
            TgClient.bot.add_handler(rename_handler)
            
            # Set timeout for rename
            async def rename_timeout():
                await sleep(60)
                if session.get('awaiting_rename'):
                    TgClient.bot.remove_handler(rename_handler)
                    session['awaiting_rename'] = False
                    if 'rename_msg' in session:
                        await delete_message(session['rename_msg'])
                    await send_audio_selection_message(session_id, message, listener.tag)
            
            from asyncio import create_task
            create_task(rename_timeout())
        
        elif action.startswith('toggle'):
            # Toggle audio track selection
            idx = int(action.split('_')[-1])
            
            if idx in session['selected_indices']:
                session['selected_indices'].remove(idx)
            else:
                session['selected_indices'].add(idx)
            
            await query.answer()
            await send_audio_selection_message(session_id, message, listener.tag)
        
        elif action == 'done':
            await query.answer()
            await delete_message(message)
            session['event'].set()
        
        else:
            await query.answer("Unknown action", show_alert=True)
    
    except Exception as e:
        LOGGER.error(f"Error in audio selection callback: {e}")
        await query.answer("An error occurred!", show_alert=True)


def setup_audio_selection_handler():
    """Setup the callback handler for audio selection"""
    TgClient.bot.add_handler(
        CallbackQueryHandler(
            audio_selection_callback,
            filters=regex("^audr_")
        )
    )

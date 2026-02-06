"""
TMV Direct Download Module
Handles /tdl command for TMV link downloads
"""

from base64 import b64encode
from re import match as re_match

from aiofiles.os import path as aiopath

from .. import DOWNLOAD_DIR, LOGGER, bot_loop, task_dict_lock
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import (
    COMMAND_USAGE,
    arg_parser,
    get_content_type,
    sync_to_async,
)
from ..helper.ext_utils.exceptions import DirectDownloadLinkException
from ..helper.ext_utils.links_utils import (
    is_gdrive_id,
    is_gdrive_link,
    is_mega_link,
    is_magnet,
    is_rclone_path,
    is_telegram_link,
    is_url,
)
from ..helper.ext_utils.task_manager import pre_task_check
from ..helper.ext_utils.tmv_scraper import scrape_tmv_link
from ..helper.listeners.task_listener import TaskListener
from ..helper.mirror_leech_utils.download_utils.aria2_download import (
    add_aria2_download,
)
from ..helper.mirror_leech_utils.download_utils.direct_downloader import (
    add_direct_download,
)
from ..helper.mirror_leech_utils.download_utils.direct_link_generator import (
    direct_link_generator,
)
from ..helper.mirror_leech_utils.download_utils.gd_download import add_gd_download
from ..helper.mirror_leech_utils.download_utils.jd_download import add_jd_download
from ..helper.mirror_leech_utils.download_utils.mega_download import add_mega_download
from ..helper.mirror_leech_utils.download_utils.nzb_downloader import add_nzb
from ..helper.mirror_leech_utils.download_utils.qbit_download import add_qb_torrent
from ..helper.mirror_leech_utils.download_utils.rclone_download import (
    add_rclone_download,
)
from ..helper.mirror_leech_utils.download_utils.telegram_download import (
    TelegramDownloadHelper,
)
from ..helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    edit_message,
    delete_message,
    get_tg_link_message,
    send_message,
)


class TMVDirect(TaskListener):
    """TMV Direct Download Handler - Always leeches"""

    def __init__(
        self,
        client,
        message,
        same_dir=None,
        bulk=None,
        multi_tag=None,
        options="",
        **kwargs,
    ):
        if same_dir is None:
            same_dir = {}
        if bulk is None:
            bulk = []
        self.message = message
        self.client = client
        self.multi_tag = multi_tag
        self.options = options
        self.same_dir = same_dir
        self.bulk = bulk
        super().__init__()
        # TMV Direct is always leech
        self.is_leech = True
        self.is_qbit = False
        self.is_jd = False
        self.is_nzb = False
        self.is_uphoster = False
        self.is_tmv = True

    async def new_event(self):
        text = self.message.text.split("\n")
        input_list = text[0].split(" ")

        check_msg, check_button = await pre_task_check(self.message)
        if check_msg:
            await delete_links(self.message)
            await auto_delete_message(
                await send_message(self.message, check_msg, check_button)
            )
            return

        args = {
            "-doc": False,
            "-med": False,
            "-d": False,
            "-j": False,
            "-s": False,
            "-b": False,
            "-e": False,
            "-z": False,
            "-sv": False,
            "-ss": False,
            "-f": False,
            "-fd": False,
            "-fu": False,
            "-hl": False,
            "-bt": False,
            "-ut": False,
            "-yt": False,
            "-i": 0,
            "-sp": 0,
            "link": "",
            "-n": "",
            "-m": "",
            "-meta": "",
            "-up": "",
            "-rcf": "",
            "-au": "",
            "-ap": "",
            "-h": "",
            "-t": "",
            "-ca": "",
            "-cv": "",
            "-ns": "",
            "-tl": "",
            "-ff": set(),
            "-audr": "",
            "-audc": "",
            "-nm": "",
        }

        arg_parser(input_list[1:], args)

        if Config.DISABLE_BULK and args.get("-b", False):
            await send_message(self.message, "Bulk downloads are currently disabled.")
            return

        if Config.DISABLE_MULTI and int(args.get("-i", 1)) > 1:
            await send_message(
                self.message,
                "Multi-downloads are currently disabled. Please try without the -i flag.",
            )
            return

        if Config.DISABLE_FF_MODE and args.get("-ff"):
            await send_message(self.message, "FFmpeg commands are currently disabled.")
            return

        self.select = args["-s"]
        self.seed = args["-d"]
        self.name = args["-n"]
        self.up_dest = args["-up"]
        self.rc_flags = args["-rcf"]
        self.link = args["link"]
        self.compress = args["-z"]
        self.extract = args["-e"]
        self.join = args["-j"]
        self.thumb = args["-t"]
        self.split_size = args["-sp"]
        self.sample_video = args["-sv"]
        self.screen_shots = args["-ss"]
        self.force_run = args["-f"]
        self.force_download = args["-fd"]
        self.force_upload = args["-fu"]
        self.convert_audio = args["-ca"]
        self.convert_video = args["-cv"]
        self.name_swap = args["-ns"]
        self.hybrid_leech = args["-hl"]
        self.thumbnail_layout = args["-tl"]
        self.as_doc = args["-doc"]
        self.as_med = args["-med"]
        self.folder_name = f"/{args['-m']}".rstrip("/") if len(args["-m"]) > 0 else ""
        self.bot_trans = args["-bt"]
        self.user_trans = args["-ut"]
        self.is_yt = args["-yt"]
        self.metadata_dict = self.default_metadata_dict.copy()
        self.audio_metadata_dict = self.audio_metadata_dict.copy()
        self.video_metadata_dict = self.video_metadata_dict.copy()
        self.subtitle_metadata_dict = self.subtitle_metadata_dict.copy()
        if args["-meta"]:
            meta = self.metadata_processor.parse_string(args["-meta"])
            self.metadata_dict = self.metadata_processor.merge_dicts(
                self.metadata_dict, meta
            )

        headers = args["-h"]
        is_bulk = args["-b"]

        bulk_start = 0
        bulk_end = 0
        ratio = None
        seed_time = None
        reply_to = None
        file_ = None
        session = ""

        try:
            self.multi = int(args["-i"])
        except Exception:
            self.multi = 0

        try:
            if args["-ff"]:
                if isinstance(args["-ff"], set):
                    self.ffmpeg_cmds = args["-ff"]
                else:
                    self.ffmpeg_cmds = eval(args["-ff"])
        except Exception as e:
            self.ffmpeg_cmds = None
            LOGGER.error(e)

        # Audio remove flag
        audr_value = args["-audr"]
        if audr_value != "" or "-audr" in " ".join(input_list):
            self.audio_remove = "interactive"
            LOGGER.info(
                f"Audio remove interactive mode enabled. Flag value: {audr_value}"
            )
        else:
            self.audio_remove = None

        self.audio_change = args["-audc"] if args["-audc"] else None
        self.new_name = args["-nm"] if args["-nm"] else None

        if not isinstance(self.seed, bool):
            dargs = self.seed.split(":")
            ratio = dargs[0] or None
            if len(dargs) == 2:
                seed_time = dargs[1] or None
            self.seed = True

        if not isinstance(is_bulk, bool):
            dargs = is_bulk.split(":")
            bulk_start = dargs[0] or 0
            if len(dargs) == 2:
                bulk_end = dargs[1] or 0
            is_bulk = True

        if not is_bulk:
            if self.multi > 0:
                if self.folder_name:
                    async with task_dict_lock:
                        if self.folder_name in self.same_dir:
                            self.same_dir[self.folder_name]["tasks"].add(self.mid)
                            for fd_name in self.same_dir:
                                if fd_name != self.folder_name:
                                    self.same_dir[fd_name]["total"] -= 1
                        elif self.same_dir:
                            self.same_dir[self.folder_name] = {
                                "total": self.multi,
                                "tasks": {self.mid},
                            }
                            for fd_name in self.same_dir:
                                if fd_name != self.folder_name:
                                    self.same_dir[fd_name]["total"] -= 1
                        else:
                            self.same_dir = {
                                self.folder_name: {
                                    "total": self.multi,
                                    "tasks": {self.mid},
                                }
                            }
                elif self.same_dir:
                    async with task_dict_lock:
                        for fd_name in self.same_dir:
                            self.same_dir[fd_name]["total"] -= 1
        else:
            await self.init_bulk(input_list, bulk_start, bulk_end, TMVDirect)
            return

        if len(self.bulk) != 0:
            del self.bulk[0]

        await self.run_multi(input_list, TMVDirect)

        await self.get_tag(text)

        path = f"{DOWNLOAD_DIR}{self.mid}{self.folder_name}"

        if not self.link and (reply_to := self.message.reply_to_message):
            if reply_to.text:
                self.link = reply_to.text.split("\n", 1)[0].strip()

        if not self.link or not is_url(self.link):
            await send_message(
                self.message, COMMAND_USAGE["tmv"][0], COMMAND_USAGE["tmv"][1]
            )
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        LOGGER.info(f"TMV Direct Link: {self.link}")

        # Send initial message
        scrape_msg = await send_message(
            self.message, "🔍 Scraping TMV download link...\nPlease wait..."
        )

        # Scrape TMV link to get direct download URL
        try:
            download_url, scraped_name, file_size = await scrape_tmv_link(self.link)
            LOGGER.info(f"Scraped download URL: {download_url}")
            LOGGER.info(f"File name: {scraped_name}, Size: {file_size}")

            # Use scraped name if user didn't provide custom name
            if not self.name and scraped_name:
                self.name = scraped_name
                LOGGER.info(f"Using scraped filename: {self.name}")

            # Update link to direct download URL
            self.link = download_url

            # Check if the scraped link is a GoFile link and process it
            if "gofile" in self.link.lower():
                LOGGER.info(
                    "Detected GoFile link, processing through direct_link_generator..."
                )
                await edit_message(scrape_msg, "🔄 Processing GoFile link...")
                try:
                    processed_link = await sync_to_async(
                        direct_link_generator, self.link
                    )
                    if isinstance(processed_link, tuple):
                        self.link, gofile_headers = processed_link
                        # Merge GoFile headers with existing headers
                        if gofile_headers:
                            headers = f"{headers} {gofile_headers}".strip()
                        LOGGER.info(f"GoFile link processed successfully")
                    elif isinstance(processed_link, str):
                        self.link = processed_link
                        LOGGER.info(f"GoFile link processed: {self.link}")
                    elif isinstance(processed_link, dict):
                        # GoFile folder with multiple files - not supported for now
                        raise Exception(
                            "GoFile folders are not supported. Please use single file links."
                        )
                except DirectDownloadLinkException as e:
                    error_msg = f"❌ Failed to process GoFile link: {str(e)}"
                    LOGGER.error(error_msg)
                    await edit_message(scrape_msg, error_msg)
                    await self.remove_from_same_dir()
                    await delete_links(self.message)
                    return
                except Exception as e:
                    error_msg = f"❌ Error processing GoFile link: {str(e)}"
                    LOGGER.error(error_msg)
                    await edit_message(scrape_msg, error_msg)
                    await self.remove_from_same_dir()
                    await delete_links(self.message)
                    return

            await delete_message(scrape_msg)

        except Exception as e:
            error_msg = f"❌ Failed to scrape TMV link: {str(e)}\n\nPlease check:\n• Link is valid and not expired\n• Site is accessible\n• Try again later"
            LOGGER.error(f"TMV Scrape Error: {e}")
            await edit_message(scrape_msg, error_msg)
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        try:
            await self.before_start()
        except Exception as e:
            await send_message(self.message, e)
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        self._set_mode_engine()

        await delete_links(self.message)

        # TMV links are always direct downloads, use aria2
        ussr = args["-au"]
        pssw = args["-ap"]
        if ussr or pssw:
            auth = f"{ussr}:{pssw}"
            headers += (
                f" authorization: Basic {b64encode(auth.encode()).decode('ascii')}"
            )

        await add_aria2_download(self, path, headers, ratio, seed_time)


async def tmv_direct_leech(client, message):
    """Handler for /tdl command"""
    if Config.DISABLE_LEECH:
        await message.reply("The Leech command is currently disabled.")
        return
    bot_loop.create_task(TMVDirect(client, message).new_event())

"""
TMV Direct Download Link Scraper
Scrapes cyberloom -> redirect -> messycloud to extract direct download links
"""

import re
from asyncio import sleep
from urllib.parse import urlparse, parse_qs, unquote

import aiohttp
from bs4 import BeautifulSoup

from ... import LOGGER


class TMVScraper:
    """Scraper for TMV download links with retry logic"""

    def __init__(self, url: str, max_retries: int = 3):
        self.url = url
        self.max_retries = max_retries
        self.session = None
        self.file_name = None
        self.file_size = None
        self.download_links = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def scrape(self) -> tuple[str, str, str]:
        """
        Main scraping method with retry logic
        Returns: (download_url, file_name, file_size_str)
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                LOGGER.info(f"TMV Scrape attempt {attempt}/{self.max_retries}")
                download_url = await self._scrape_download_link()
                if download_url:
                    return download_url, self.file_name, self.file_size
                LOGGER.warning(f"Attempt {attempt} failed, no download link found")
            except Exception as e:
                LOGGER.error(f"TMV Scrape attempt {attempt} error: {e}")
                if attempt < self.max_retries:
                    await sleep(2 * attempt)  # Exponential backoff
                else:
                    raise Exception(
                        f"Failed to scrape after {self.max_retries} attempts: {e}"
                    )

        raise Exception("No download link found after all retry attempts")

    async def _scrape_download_link(self) -> str:
        """Complete scraping flow: Initial -> Redirect -> Final"""
        # Step 1: Get initial page and find redirect link
        redirect_url = await self._get_redirect_url()
        if not redirect_url:
            raise Exception("No redirect URL found in initial page")

        LOGGER.info(f"Found redirect URL: {redirect_url}")

        # Step 2: Follow redirect and get download page URL
        download_page_url = await self._get_download_page_url(redirect_url)
        if not download_page_url:
            raise Exception("No download page URL found")

        LOGGER.info(f"Found download page URL: {download_page_url}")

        # Step 3: Parse download page and extract best link
        download_url = await self._extract_best_download_link(download_page_url)
        if not download_url:
            raise Exception("No suitable download link found on final page")

        return download_url

    async def _get_redirect_url(self) -> str:
        """Step 1: Fetch initial page and extract redirect URL"""
        async with self.session.get(self.url) as response:
            if response.status != 200:
                raise Exception(f"Initial page returned status {response.status}")

            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")

            # Find the CTA link
            cta_link = soup.find("a", id="cta")
            if cta_link and cta_link.get("href"):
                return cta_link["href"]

            # Fallback: try to find any redirect link
            for link in soup.find_all("a", href=True):
                if "redirect" in link["href"].lower():
                    return link["href"]

            return None

    async def _get_download_page_url(self, redirect_url: str) -> str:
        """Step 2: Follow redirect and extract download page URL with metadata"""
        async with self.session.get(redirect_url, allow_redirects=True) as response:
            if response.status != 200:
                raise Exception(f"Redirect page returned status {response.status}")

            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")

            # Extract file metadata
            file_name_el = soup.find("div", class_="file-name")
            if file_name_el:
                raw_filename = file_name_el.text.strip()
                # Clean filename: remove domain patterns like "www.1TamilMV.LC - "
                self.file_name = re.sub(
                    r"^(?:www\.)?[\w\-]+\.[\w\.]+\s*-?\s*", "", raw_filename
                )
            else:
                self.file_name = None

            file_meta_el = soup.find("div", class_="file-meta")
            if file_meta_el:
                self.file_size = file_meta_el.text.strip()

            # Find the continue button
            download_btn = soup.find("a", id="continue-btn")
            if download_btn and download_btn.get("href"):
                return download_btn["href"]

            # Fallback: find any button with download-related class
            for btn in soup.find_all(
                "a", class_=re.compile(r"(continue|download|btn)", re.I)
            ):
                if btn.get("href"):
                    return btn["href"]

            return None

    async def _extract_best_download_link(self, download_page_url: str) -> str:
        """Step 3: Parse messycloud page and extract best download link with proper wait time"""
        async with self.session.get(
            download_page_url, allow_redirects=True
        ) as response:
            if response.status != 200:
                raise Exception(f"Download page returned status {response.status}")

            html = await response.text()

        # Wait for page to fully load (important for JavaScript-rendered content)
        LOGGER.info("Waiting for download page to fully load...")
        await sleep(1)  # Initial wait for page load

        # Fetch the page again after waiting to ensure dynamic content is loaded
        async with self.session.get(
            download_page_url, allow_redirects=True
        ) as response:
            if response.status != 200:
                raise Exception(f"Download page returned status {response.status}")

            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")

            # Find all download buttons with retry logic
            download_buttons = soup.find_all("a", class_="download-btn")

            # If no buttons found, try waiting a bit more and retry
            if not download_buttons:
                LOGGER.warning(
                    "No download buttons found on first attempt, waiting longer..."
                )
                await sleep(2)

                async with self.session.get(
                    download_page_url, allow_redirects=True
                ) as retry_response:
                    if retry_response.status == 200:
                        html = await retry_response.text()
                        soup = BeautifulSoup(html, "html.parser")
                        download_buttons = soup.find_all("a", class_="download-btn")

            if not download_buttons:
                raise Exception("No download buttons found on page after waiting")

            LOGGER.info(f"Found {len(download_buttons)} download links")

            # Categorize links by priority: GoFile > M1 > MS (only these 3)
            gofile_links = []
            m1_links = []
            ms_links = []

            for btn in download_buttons:
                btn_text = btn.get_text(strip=True)
                has_resume = btn.find("span", class_="resume-yes") is not None
                href = btn.get("href", "")

                # Construct full URL if relative
                if href.startswith("/"):
                    base_url = f"{urlparse(download_page_url).scheme}://{urlparse(download_page_url).netloc}"
                    full_url = base_url + href
                else:
                    full_url = href

                # Extract actual download URL from query parameter
                actual_url = self._extract_actual_url(full_url)

                link_info = {
                    "text": btn_text,
                    "url": actual_url,
                    "has_resume": has_resume,
                }

                btn_lower = btn_text.lower()
                url_lower = actual_url.lower()

                # Priority 1: GoFile
                if "gofile" in btn_lower or "gofile.io" in url_lower:
                    gofile_links.append(link_info)
                    LOGGER.info(f"Found GoFile link: {btn_text}")
                # Priority 2: M1 Direct
                elif "[m1]" in btn_lower or "m1" in btn_lower:
                    m1_links.append(link_info)
                    LOGGER.info(f"Found M1 link: {btn_text}")
                # Priority 3: MS (Messycloud/other MS servers)
                elif (
                    "[ms]" in btn_lower
                    or "ms" in btn_lower
                    or "messycloud" in url_lower
                ):
                    ms_links.append(link_info)
                    LOGGER.info(f"Found MS link: {btn_text}")

            # Strict Priority: GoFile > M1 > MS (ONLY - no other links allowed)
            if gofile_links:
                selected = gofile_links[0]
                LOGGER.info(f"✅ Selected GoFile link: {selected['text']}")
                return selected["url"]
            elif m1_links:
                selected = m1_links[0]
                LOGGER.info(f"✅ Selected M1 link: {selected['text']}")
                return selected["url"]
            elif ms_links:
                selected = ms_links[0]
                LOGGER.info(f"✅ Selected MS link: {selected['text']}")
                return selected["url"]
            else:
                raise Exception(
                    "❌ No supported download links found!\nOnly GoFile, M1, and MS links are supported."
                )

    def _extract_actual_url(self, full_url: str) -> str:
        """Extract actual download URL from query parameter"""
        parsed_url = urlparse(full_url)
        query_params = parse_qs(parsed_url.query)

        if "url" in query_params:
            return unquote(query_params["url"][0])

        return full_url


async def scrape_tmv_link(url: str) -> tuple[str, str, str]:
    """
    Convenience function to scrape TMV link
    Returns: (download_url, file_name, file_size)
    """
    async with TMVScraper(url) as scraper:
        return await scraper.scrape()

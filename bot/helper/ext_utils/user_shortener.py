from base64 import b64encode
from random import choice, random
from asyncio import sleep as asleep
from urllib.parse import quote

from cloudscraper import create_scraper
from urllib3 import disable_warnings

from ... import LOGGER


async def user_short_url(longurl, user_dict, attempt=0):
    """Shorten URL using user-specific shortener config"""
    
    # Check if user has shortener enabled and configured
    if not user_dict.get("SHORTENER_ENABLED", False):
        return longurl
    
    shortener_url = user_dict.get("SHORTENER_URL", "")
    shortener_api = user_dict.get("SHORTENER_API", "")
    
    if not shortener_url or not shortener_api:
        return longurl
    
    if attempt >= 4:
        return longurl

    cget = create_scraper().request
    disable_warnings()
    
    try:
        if "shorte.st" in shortener_url:
            headers = {"public-api-token": shortener_api}
            data = {"urlToShorten": quote(longurl)}
            return cget(
                "PUT", "https://api.shorte.st/v1/data/url", headers=headers, data=data
            ).json()["shortenedUrl"]
        elif "linkvertise" in shortener_url:
            url = quote(b64encode(longurl.encode("utf-8")))
            linkvertise = [
                f"https://link-to.net/{shortener_api}/{random() * 1000}/dynamic?r={url}",
                f"https://up-to-down.net/{shortener_api}/{random() * 1000}/dynamic?r={url}",
                f"https://direct-link.net/{shortener_api}/{random() * 1000}/dynamic?r={url}",
                f"https://file-link.net/{shortener_api}/{random() * 1000}/dynamic?r={url}",
            ]
            return choice(linkvertise)
        elif "bitly.com" in shortener_url:
            headers = {"Authorization": f"Bearer {shortener_api}"}
            return cget(
                "POST",
                "https://api-ssl.bit.ly/v4/shorten",
                json={"long_url": longurl},
                headers=headers,
            ).json()["link"]
        elif "ouo.io" in shortener_url:
            return cget(
                "GET", f"http://ouo.io/api/{shortener_api}?s={longurl}", verify=False
            ).text
        elif "cutt.ly" in shortener_url:
            return cget(
                "GET",
                f"http://cutt.ly/api/api.php?key={shortener_api}&short={longurl}",
            ).json()["url"]["shortLink"]
        else:
            res = cget(
                "GET",
                f"https://{shortener_url}/api?api={shortener_api}&url={quote(longurl)}",
            ).json()
            shorted = res["shortenedUrl"]
            if not shorted:
                shrtco_res = cget(
                    "GET", f"https://api.shrtco.de/v2/shorten?url={quote(longurl)}"
                ).json()
                shrtco_link = shrtco_res["result"]["full_short_link"]
                res = cget(
                    "GET",
                    f"https://{shortener_url}/api?api={shortener_api}&url={shrtco_link}",
                ).json()
                shorted = res["shortenedUrl"]
            if not shorted:
                shorted = longurl
            return shorted
    except Exception as e:
        LOGGER.error(f"User shortener error: {e}")
        await asleep(0.8)
        attempt += 1
        return await user_short_url(longurl, user_dict, attempt)

import asyncio
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

URLS = ["https://gymshark.com", "https://www.fnac.com", "https://www.amazon.fr"]

async def main():
    async with AsyncWebCrawler() as c:
        for u in URLS:
            r = await c.arun(url=u, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS))
            md = getattr(r.markdown, "raw_markdown", None) or str(r.markdown or "")
            print(u, "OK" if r.success else "ECHEC", len(md), "caractères")
            print(md[:300].replace("\n", " "))
            print("liens internes:", len((r.links or {}).get("internal", [])))

asyncio.run(main())

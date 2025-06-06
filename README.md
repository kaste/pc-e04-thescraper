Scraping packagecontrol.io because we don't have access to the old db.


```
uv run -m scripts.scrape --limit 20
```


You need the registry from https://github.com/packagecontrol/thecrawl.
Download it from
https://github.com/packagecontrol/thecrawl/releases/download/crawler-status/registry.json

or make it fresh if you have the crawl downloaded elsewhere:

```
uv run -m scripts.generate_registry
```

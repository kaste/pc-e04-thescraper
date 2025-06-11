Scraping packagecontrol.io because we don't have access to the old db.


```
uv run -m scripts.scrape --limit 20
```

You need the registry from https://github.com/packagecontrol/thecrawl.
Download it using [gh](https://cli.github.com/):

```
gh release -R https://github.com/packagecontrol/thecrawl download crawler-status --pattern="registry.json"
```

Or just browse the website.

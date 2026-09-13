# getAWSdocs.py

## About

One thing that strikes me as odd with Amazon and the documentation on AWS is that there is no download all button, to make it easy to get all the documentation in one go. After creating a simple bash script that kept breaking and needed updating, I decided to rewrite in python to make it a little easier to maintain. You can download Documentation and/or WhitePapers. The script is now ported to Python3 (finally)!

I hope some of you find this useful.

## Requirements

You need Python3 plus two third party modules:

 - beautifulsoup4
 - lxml (provides the `xml` parser beautifulsoup4 is asked for)

Everything else the script uses (`argparse`, `json`, `os`, `urllib`) is part of the Python standard library.

example:

```bash
sudo pip install -r requirements.txt
```

## Usage

To get all documents as PDFs:

```
./getAWSdocs.py -d
```

Downloading all the docs (390+ at the time of writing) still takes a while even though the crawl runs concurrently. PDFs are saved under `documentation/`, mirroring the path of each guide on docs.aws.amazon.com.

`-d` also takes an optional format. `-d pdf` is the same as a bare `-d`. The other two formats download the individual pages of each guide instead of one file per guide, discovered from the guide's `sitemap.xml`:

```
./getAWSdocs.py -d html
./getAWSdocs.py -d md
```

`-d html` saves the pages as HTML under `documentation/html/`. `-d md` fetches AWS' markdown rendition of each page (the same URL with `.md` in place of `.html`) and saves it under `documentation/markdown/`.

__Note:__ Both of these download every page of every guide, so they pull down orders of magnitude more files and take much longer than the PDF run. For HTML, only the pages themselves are saved - stylesheets, images and scripts are not fetched and links are not rewritten, so the result is a text archive rather than a browsable offline copy. Markdown avoids that problem entirely and the files are smaller, so it is the better choice if you just want the text.

To get all whitepapers:

```
./getAWSdocs.py -w
```

Whitepapers are saved under `whitepapers/`.

Files that exist on disk will not be re-downloaded (so by default only new sections/files are downloaded), whichever format you pick. To override this default and force re-download of files that exist on disk, use

```bash
./getAWSdocs.py -d -f
```

__Note:__ You can use a combination of -d and -w to download all documents at once.

### Limiting the request rate

The crawl throttles itself to a maximum number of requests per second, shared across all workers, so a full run stays polite toward docs.aws.amazon.com. The default is **3 req/sec**. Change it with `-r`/`--rate`:

```bash
./getAWSdocs.py -d -r 8
```

The rate must be greater than 0 and at most 16; anything outside that range aborts with an error. See [Speed](#speed) for how this interacts with the worker count.

## How it works

For documentation (`-d`), the script walks the AWS docs catalogue in stages:

1. It fetches the top-level landing page (`main-landing-page.xml`) and reads the list of services from it.
2. **Resolve services (concurrent):** every service link is resolved in parallel to its set of guide-root URLs. A few special cases are handled here - in PDF mode, a service link that points straight at an HTML page listing PDFs is scraped for those PDFs directly; when a service page exposes no modern guide links, the script falls back to the older XML "tile" landing pages; and the service link itself is also treated as a candidate guide root.
3. **Fetch guide files (concurrent):** every discovered guide root is scanned in parallel. For PDFs it reads a small `meta-inf/guide-info.json` file to find the guide's PDF URL; for `html`/`md` it reads the guide's `sitemap.xml` to enumerate every page.
4. **Download (concurrent):** each file is downloaded and saved under `documentation/`, mirroring the guide's path on docs.aws.amazon.com.

For whitepapers (`-w`) discovery is simpler: it scrapes two AWS whitepapers index pages - the general index and the SAP whitepapers index - for links ending in `.pdf` (matching `whitepapers` or `enterprise-marketing` URLs) and downloads them into `whitepapers/`.

## Speed

Discovery is thousands of small HTTP requests, and the work is dominated by network round-trip latency rather than bandwidth or CPU. To keep the total time reasonable across 390+ guides, the crawl runs concurrently: service resolution, per-guide file discovery, and downloads each use a thread pool (`MAX_WORKERS = 16`). Even so, a full run - especially the `html`/`md` modes, which pull every page of every guide - still touches an enormous number of URLs and can take a long time.

Two separate knobs govern the traffic, and they do different things:

 - **`--rate` (requests per second, default 3)** caps *throughput* - how many requests start each second, across all workers combined. This is the politeness throttle, and it is the binding limit for a typical run. It must be `> 0` and `<= 16`; a higher value aborts with an error.
 - **`MAX_WORKERS = 16`** caps *concurrency* - how many requests are in flight at once. Because each request blocks on network latency, you need several workers in parallel just to reach the target rate; the pool is also what stops an enormous number of requests launching at once. At the default rate the rate limiter binds first and most of the pool sits idle.

Raising the rate too far risks getting your IP rate-limited or having the traffic flagged as abusive, which is why it is capped at 16.

That's it!

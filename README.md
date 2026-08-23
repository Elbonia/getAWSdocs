# getAWSdocs.py

## About

One thing that strikes me as odd with Amazon and the documentation on AWS is that there is no download all button, to make it easy to get all the documentation in one go. After creating a simple bash script that kept breaking and needed updating, I decided to rewrite in python to make it a little easier to maintain. You can download Documentation and/or WhitePapers as PDFs. The script is now ported to Python3 (finally)!

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

Downloading all the docs (390+ at the time of writing) can take a very long time - potentially days. PDFs are saved under `documentation/`, mirroring the path of each guide on docs.aws.amazon.com.

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

Files that exist on disk will not be re-downloaded (so by default only new sections/files are downloaded). To override this default and force re-download of files that exist on disk, use

```bash
./getAWSdocs.py -d -f
```

__Note:__ You can use a combination of -d and -w to download all documents at once.

## How it works

For documentation (`-d`), the script walks the AWS docs catalogue in three stages, all done one request at a time:

1. It fetches the top-level landing page and reads the list of services from it.
2. For each service it fetches that service's landing page, and for each guide listed there it fetches a small `meta-inf/guide-info.json` file to find the URL of the guide's PDF.
3. It then downloads each PDF and saves it under `documentation/`, mirroring the guide's path on docs.aws.amazon.com.

For whitepapers (`-w`) discovery is simpler: it scrapes the AWS whitepapers index pages directly for links ending in `.pdf` and downloads them into `whitepapers/`.

## Why it's slow

Discovery alone is thousands of small, sequential HTTP requests, and each one waits for the previous to finish before the next begins. Because the work is dominated by network round-trip latency rather than bandwidth, the total time is roughly the number of requests multiplied by the per-request latency - which, across 390+ guides, can stretch into hours or days.

This is a deliberate design decision, not an oversight. Making the requests one at a time keeps the load on Amazon's servers low. Firing them off in parallel would be far faster, but hammering docs.aws.amazon.com with a burst of concurrent requests risks getting your IP rate-limited or banned, or having the traffic flagged as a denial-of-service attack. Running slowly and politely is the safer way to pull the whole catalogue.

That's it!

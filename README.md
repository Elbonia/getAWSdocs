# getAWSdocs.py

## About

One thing that strikes me as odd with Amazon and the documentation on AWS is that there is no download all button, to make it easy to get all the documentation in one go. After creating a simple bash script that kept breaking and needed updating, I decided to rewrite in python to make it a little easier to maintain. You can download Documentation and/or WhitePapers. The script is now ported to Python3 (finally)!

I hope some of you find this useful.

## Requirements

Python3, plus two third party modules:

 - beautifulsoup4
 - lxml (used for the `xml` parser beautifulsoup4 is asked for)

Everything else the script imports - `argparse`, `json`, `os`, `urllib` - is in the standard library.

example:

```bash
sudo pip install -r requirements.txt
```

## Usage

To get all documents as PDFs:

```
./getAWSdocs.py -d
```

Downloading all the docs (290 at the time of writting) can take a long time ~20mins. PDFs are saved under `documentation/`, mirroring the path of each guide on docs.aws.amazon.com.

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

That's it!

Built by Ric: [@ric__harvey](https://twitter.com/ric__harvey)

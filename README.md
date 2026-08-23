# getAWSdocs.py

## About

One thing that strikes me as odd with Amazon and the documentation on AWS is that there is no download all button, to make it easy to get all the documentation in one go. After creating a simple bash script that kept breaking and needed updating, I decided to rewrite in python to make it a little easier to maintain. You can download Documentation and/or WhitePapers. The script is now pported to Python3 (finally)!

I hope some of you find this useful.

## Requirements

Make sure all these python modules are installed as well as Python3:

 - argparse
 - beautifulsoup4
 - urllib3+
 - urlparse3
 - lxml

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

`-d` also takes an optional format. `-d pdf` is the same as a bare `-d`; `-d html` downloads the individual HTML pages of each guide instead, discovered from the guide's `sitemap.xml`, and saves them under `documentation/html/`:

```
./getAWSdocs.py -d html
```

__Note:__ HTML mode downloads every page of every guide rather than one file per guide, so it pulls down orders of magnitude more files and takes much longer than the PDF run. Only the pages themselves are saved - stylesheets, images and scripts are not fetched and links are not rewritten, so the result is a text archive rather than a browsable offline copy.

To get all whitepapers:

```
./getAWSdocs.py -w
```

Files that exist on disk will not be re-downloaded (so by default only new sections/files are downloaded). To override this default and force re-download of files that exist on disk, use

```bash
./getAWSdocs.py -d -f
```

__Note:__ You can use a combination of -d and -w to download all documents at once.

Thats it!

Built by Ric: [@ric__harvey](https://twitter.com/ric__harvey)

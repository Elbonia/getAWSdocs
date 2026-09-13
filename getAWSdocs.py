#!/usr/bin/env python3

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, urlsplit
from urllib.request import urlopen

from bs4 import BeautifulSoup

URL_TIMEOUT = 20
# Every network call in this script is a small text/HTML/JSON/XML fetch, so
# the work is latency-bound rather than bandwidth- or CPU-bound. A run
# against the full AWS docs catalog touches many thousands of URLs, so a
# higher worker count matters a lot more here than for a handful of files.
MAX_WORKERS = 16
# Default cap on total request throughput, in requests per second, applied
# across all workers combined (overridable with --rate). This is a
# politeness/rate limit, not a performance target: with MAX_WORKERS threads
# all issuing requests, an uncapped run can hammer docs.aws.amazon.com hard
# enough to get throttled or blocked.
DEFAULT_RATE = 3.0
# Hard ceiling on --rate. docs.aws.amazon.com is a shared public service;
# anything above this is treated as an error rather than silently clamped so
# the caller knows their setting was rejected.
MAX_RATE = 16.0


class RateLimiter:
    """Thread-safe cap on request throughput, shared across every worker.

    All network fetches acquire from a single instance, so the cap holds
    globally rather than per thread pool. Each caller reserves the next
    evenly-spaced slot under the lock and then sleeps outside it, so workers
    still overlap on network latency while requests leave at the target rate.
    """

    def __init__(self, max_per_second):
        self.min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_time = 0.0

    def acquire(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_time)
            self._next_time = scheduled + self.min_interval
        wait = scheduled - now
        if wait > 0:
            time.sleep(wait)


# Configured from --rate in main() before any fetching begins.
_rate_limiter = RateLimiter(DEFAULT_RATE)


def fetch(url, timeout=URL_TIMEOUT):
    """Rate-limited wrapper around urlopen used for every network request."""
    _rate_limiter.acquire()
    return urlopen(url, timeout=timeout)


def get_options():
    parser = argparse.ArgumentParser(description="AWS Documentation Downloader")
    parser.add_argument(
        "-d",
        "--documentation",
        help=
        "Download the Documentation, optionally as 'pdf' (default), 'html' or 'md'",
        nargs="?",
        const="pdf",
        default=None,
        choices=["pdf", "html", "md"],
        required=False,
    )
    parser.add_argument(
        "-w",
        "--whitepapers",
        help="Download White Papers",
        action="store_true",
        required=False,
    )
    parser.add_argument("-f",
                        "--force",
                        help="Overwrite old files",
                        action="store_true",
                        required=False)
    parser.add_argument(
        "-r",
        "--rate",
        help="Maximum requests per second across all workers "
        "(default: %(default)s, must be > 0 and <= " + str(int(MAX_RATE)) + ")",
        type=float,
        default=DEFAULT_RATE,
        required=False,
    )
    args = parser.parse_args()
    if args.rate <= 0 or args.rate > MAX_RATE:
        parser.error("--rate must be greater than 0 and at most %g (got %g)" %
                     (MAX_RATE, args.rate))
    if not args.documentation and not args.whitepapers:
        parser.print_help()
    return vars(args)


# Build a list of the amazon PDF's
def list_whitepaper_pdfs(start_page):
    html_page = fetch(start_page, timeout=URL_TIMEOUT)
    # Parse the HTML page
    soup = BeautifulSoup(html_page, "html.parser")
    pdfs = set()
    print("Generating PDF list (this may take some time)")
    for link in soup.find_all("a"):
        try:
            uri = link.get("href")
            print("URI: ", uri)
            # Allow whitepapers to be returned
            if "whitepapers" in start_page:
                if uri.endswith("pdf"):
                    if "whitepapers" in uri or "enterprise-marketing" in uri:
                        pdfs.add(uri)
        except Exception:
            continue
    return pdfs


def find_pdfs_in_html(url):
    """Scrape a page directly for links ending in 'pdf'.

    Used when a top-level service link points straight at an HTML page
    (rather than at a guide root), which is the one case a guide's
    meta-inf/guide-info.json lookup can't discover.
    """
    try:
        page = fetch(url, timeout=URL_TIMEOUT)
        soup = BeautifulSoup(page, "html.parser")
        return {
            urljoin(url,
                    link.get("href").split("?")[0])
            for link in soup.find_all("a", href=True)
            if link.get("href").split("?")[0].endswith("pdf")
        }
    except Exception:
        return set()


def get_guide_pdf(guide_url, base_url):
    guide_info_url = urljoin(guide_url, "meta-inf/guide-info.json")
    try:
        print("Guide info url:", guide_info_url)
        guide_info_doc = fetch(guide_info_url, timeout=URL_TIMEOUT).read()
        guide_info = json.loads(guide_info_doc)
        if "pdf" in guide_info and guide_info["pdf"]:
            return urljoin(base_url, guide_info["pdf"])
    except Exception:
        return None
    return None


def list_guide_urls(service_url, base_url):
    guide_urls = set()
    service_page = fetch(service_url, timeout=URL_TIMEOUT)
    service_soup = BeautifulSoup(service_page, "html.parser")
    for guide_link in service_soup.find_all("a", href=True):
        guide_url = urljoin(base_url, guide_link.get("href").split("?")[0])
        if urlparse(guide_url).netloc != "docs.aws.amazon.com":
            continue
        # Guide roots are directories: sitemap.xml/meta-inf/guide-info.json
        # are resolved relative to them with urljoin(), which treats a URL
        # without a trailing slash as a *file* and resolves relative paths
        # against its parent directory instead. Normalize so that lookup
        # isn't silently pointed at the wrong directory.
        last_segment = urlparse(guide_url).path.rsplit("/", 1)[-1]
        if last_segment and "." not in last_segment:
            guide_url += "/"
        guide_urls.add(guide_url)
    return guide_urls


def get_guide_html_pages(guide_url, base_url):
    sitemap_url = urljoin(guide_url, "sitemap.xml")
    try:
        sitemap_doc = fetch(sitemap_url, timeout=URL_TIMEOUT).read()
        soup = BeautifulSoup(sitemap_doc, "xml")
        return {loc.text.strip() for loc in soup.find_all("loc") if loc.text}
    except Exception:
        return set()


def get_guide_md_pages(guide_url, base_url):
    # AWS serves a markdown rendition of each page at the same path with a
    # .md extension in place of .html. Pages that don't end in .html (e.g.
    # directory-style sitemap entries) have no known markdown URL, so they
    # are dropped rather than downloaded as-is and mislabeled with a .md
    # extension.
    return {
        page[:-len(".html")] + ".md"
        for page in get_guide_html_pages(guide_url, base_url)
        if page.endswith(".html")
    }


def get_guide_pdf_files(guide_url, base_url):
    pdf_url = get_guide_pdf(guide_url, base_url)
    return {pdf_url} if pdf_url else set()


def _legacy_tile_guide_urls(uri, service_url, base_url, locale_path):
    """Older docs landing pages exposed guides via XML 'tile' links instead
    of HTML anchors. Used as a fallback when the modern HTML scrape finds
    no guide links at all."""
    guide_urls = set()
    try:
        if not uri.startswith("http"):
            url = base_url + uri.split(
                "?")[0] + locale_path + "landing-page.xml"
        else:
            url = uri.split("?")[0]
        sub_page_doc = fetch(url, timeout=URL_TIMEOUT)
        soup_doc = BeautifulSoup(sub_page_doc, "xml")
        for sublink in soup_doc.find_all("tile"):
            try:
                sub_url = sublink.get("href")
                directory = base_url + "/".join(
                    urlsplit(sub_url).path.split("/")[:-1])
                guide_urls.add(directory + "/")
            except Exception:
                continue
    except Exception as exc:
        print("Skipping " + service_url + " - " + str(exc))
    return guide_urls


def _resolve_service_guides(uri, base_url, locale_path, is_pdf_mode):
    """Network-bound step: turn one top-level service link into either a set
    of guide-root URLs to scan (phase 2), or - for a link that points
    straight at an HTML page listing PDFs - the PDF URLs themselves."""
    print("URI: ", uri)
    service_url = urljoin(base_url, uri.split("?")[0])
    if urlparse(service_url).netloc != "docs.aws.amazon.com":
        print("Skipping external URL: " + service_url)
        return set(), set()

    # A top-level link can point straight at an HTML page (rather than a
    # guide root) that lists PDFs directly; that's only meaningful for PDF
    # discovery, and a guide-info.json lookup against it wouldn't find
    # anything anyway.
    if is_pdf_mode and ".html" in uri:
        return set(), find_pdfs_in_html(service_url)

    guide_urls = set()
    try:
        guide_urls = list_guide_urls(service_url, base_url)
    except Exception as exc:
        print("Guide link scrape failed for " + service_url + " - " + str(exc))

    if not guide_urls:
        # An empty result here is not an error (BeautifulSoup finding zero
        # matching anchors doesn't raise), so this fallback must be tried
        # whenever nothing was found, not only when list_guide_urls() raises.
        guide_urls = _legacy_tile_guide_urls(uri, service_url, base_url,
                                             locale_path)
        if not guide_urls:
            print("No guide links or legacy tiles found for " + service_url)

    # The service link itself is also a candidate guide root - current docs
    # pages expose PDFs/HTML through each guide's own metadata.
    guide_urls.add(service_url)
    return guide_urls, set()


def list_docs_files(start_page, get_guide_files):
    locale_path = "en_us/"
    base_url = "https://docs.aws.amazon.com"

    page = fetch(start_page, timeout=URL_TIMEOUT)
    soup = BeautifulSoup(page, "xml")
    files = set()
    print("Generating file list (this may take some time)")

    service_links = soup.find_all("service")
    if not service_links:
        service_links = soup.find_all("list-card-item")
    print("Found " + str(len(service_links)) + " documentation entries")

    uris = [link.get("href") for link in service_links if link.get("href")]
    is_pdf_mode = get_guide_files is get_guide_pdf_files

    # Phase 1: resolve every top-level service link to its guide-root URLs.
    # This is pure network I/O (one request per service), so all services
    # are resolved concurrently in a single pool instead of one at a time -
    # a per-service pool here would only parallelize a service's own guides
    # against each other, leaving the (much larger) cross-service work
    # serialized.
    guide_urls = set()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_uri = {
            executor.submit(_resolve_service_guides, uri, base_url, locale_path, is_pdf_mode):
                uri for uri in uris
        }
        for future in as_completed(future_to_uri):
            try:
                guides, direct_files = future.result()
                guide_urls.update(guides)
                files.update(direct_files)
            except Exception as exc:
                print("Skipping " + future_to_uri[future] + " - " + str(exc))

    print("Found " + str(len(guide_urls)) + " guide roots to scan")

    # Phase 2: fetch each guide's files. Also pure network I/O, so every
    # guide discovered across every service is scanned concurrently in one
    # pool, rather than one service's guides at a time.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_guide = {
            executor.submit(get_guide_files, guide_url, base_url): guide_url
            for guide_url in guide_urls
        }
        for future in as_completed(future_to_guide):
            try:
                files.update(future.result())
            except Exception as exc:
                print("Skipping guide " + future_to_guide[future] + " - " +
                      str(exc))

    print("Found " + str(len(files)) + " documentation files")
    return files


def list_docs_pdfs(start_page):
    return list_docs_files(start_page, get_guide_pdf_files)


def list_docs_html(start_page):
    return list_docs_files(start_page, get_guide_html_pages)


def list_docs_md(start_page):
    return list_docs_files(start_page, get_guide_md_pages)


def save_pdf(full_dir, filename, i, force):
    # exist_ok avoids a race when concurrent downloads share a directory.
    os.makedirs(full_dir, exist_ok=True)
    # Open the URL and retrieve data
    file_loc = full_dir + filename
    if not os.path.exists(file_loc) or force:
        if i.startswith("//"):
            i = "http:" + i
        print("Downloading : " + i)
        with fetch(i, timeout=URL_TIMEOUT) as web:
            print("Saving to : " + file_loc)
            # Save Data to disk
            with open(file_loc, "wb") as output:
                output.write(web.read())
    else:
        print(
            "Skipping " + i +
            " - file exists or is a dated API document, use './getAWSdocs.py --force' to force override"
        )


# Per-page documentation modes: file extension and download directory.
PAGE_MODES = {
    "html": (".html", "documentation/html/"),
    "md": (".md", "documentation/markdown/"),
}


def page_filename(url, extension):
    """Map a documentation page URL onto a local filename."""
    filename = urlsplit(url).path.split("/")[-1]
    if not filename:
        # Directory-style URL, e.g. .../userguide/
        return "index" + extension
    if "." not in filename:
        return filename + extension
    return filename


def get_pdfs(pdf_list, force, mode="pdf"):
    extension, page_dir = PAGE_MODES.get(mode, (None, None))

    def download_one(i):
        doc = i.split("/")
        if len(doc) < 4:
            # Not an absolute http(s)://host/path/... URL (e.g. a malformed
            # or relative <loc> entry from a sitemap.xml) - skip it instead
            # of crashing the rest of the run on an IndexError.
            print("Skipping " + i + " - unexpected URL format")
            return
        doc_location = doc[3]
        if extension:
            filename = page_filename(i, extension)
        else:
            filename = urlsplit(i).path.split("/")[-1]
        # Set download dir for whitepapers
        if "whitepapers" in doc_location:
            full_dir = "whitepapers/"
        else:
            # Set download dir and sub directories for documentation
            full_dir = page_dir or "documentation/"
            # Trailing "" for directory-style URLs is dropped along with the
            # filename, so the full path becomes the directory.
            directory = urlsplit(i).path.split("/")[:-1]
            for path in directory:
                if path != "":
                    full_dir = full_dir + path + "/"
        try:
            save_pdf(full_dir, filename, i, force)
        except Exception:
            return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        list(executor.map(download_one, pdf_list))


def main():
    args = get_options()
    # allow user to overwrite files
    force = args["force"]
    # Configure the shared rate limiter before any fetching begins.
    global _rate_limiter
    _rate_limiter = RateLimiter(args["rate"])
    pdf_list = set()
    if args["documentation"]:
        print("Downloading Docs")
        landing_page = "https://docs.aws.amazon.com/en_us/main-landing-page.xml"
        mode = args["documentation"]
        if mode == "html":
            docs_file_list = list_docs_html(landing_page)
        elif mode == "md":
            docs_file_list = list_docs_md(landing_page)
        else:
            docs_file_list = list_docs_pdfs(landing_page)
        pdf_list.update(docs_file_list)
        get_pdfs(docs_file_list, force, mode=mode)

    if args["whitepapers"]:
        print("Downloading Whitepapaers")
        whitepaper_pdf_list = list_whitepaper_pdfs(
            "http://aws.amazon.com/whitepapers/")
        pdf_list.update(whitepaper_pdf_list)
        get_pdfs(whitepaper_pdf_list, force)
        print("Downloading SAP Whitepapaers")
        sap_pdf_list = list_whitepaper_pdfs(
            "https://aws.amazon.com/sap/whitepapers/")
        pdf_list.update(sap_pdf_list)
        get_pdfs(sap_pdf_list, force)

    for p in pdf_list:
        print(p)


if __name__ == "__main__":
    main()

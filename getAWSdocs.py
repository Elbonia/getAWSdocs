#!/usr/bin/env python3

import argparse
import json
import os
from urllib.parse import urljoin, urlparse, urlsplit
from urllib.request import urlopen

from bs4 import BeautifulSoup

URL_TIMEOUT = 20


def get_options():
    parser = argparse.ArgumentParser(description="AWS Documentation Downloader")
    parser.add_argument(
        "-d",
        "--documentation",
        help="Download the Documentation, optionally as 'pdf' (default) or 'html'",
        nargs="?",
        const="pdf",
        default=None,
        choices=["pdf", "html"],
        required=False,
    )
    parser.add_argument(
        "-w",
        "--whitepapers",
        help="Download White Papers",
        action="store_true",
        required=False,
    )
    parser.add_argument(
        "-f", "--force", help="Overwrite old files", action="store_true", required=False
    )
    args = parser.parse_args()
    if not args.documentation and not args.whitepapers:
        parser.print_help()
    return vars(args)


# Build a list of the amazon PDF's
def list_whitepaper_pdfs(start_page):
    html_page = urlopen(start_page, timeout=URL_TIMEOUT)
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


def get_guide_pdf(guide_url, base_url):
    guide_info_url = urljoin(guide_url, "meta-inf/guide-info.json")
    try:
        print("Guide info url:", guide_info_url)
        guide_info_doc = urlopen(guide_info_url, timeout=URL_TIMEOUT).read()
        guide_info = json.loads(guide_info_doc)
        if "pdf" in guide_info and guide_info["pdf"]:
            return urljoin(base_url, guide_info["pdf"])
    except Exception:
        return None
    return None


def list_guide_urls(service_url, base_url):
    guide_urls = set()
    service_page = urlopen(service_url, timeout=URL_TIMEOUT)
    service_soup = BeautifulSoup(service_page, "html.parser")
    for guide_link in service_soup.find_all("a", href=True):
        guide_url = urljoin(base_url, guide_link.get("href").split("?")[0])
        if urlparse(guide_url).netloc == "docs.aws.amazon.com":
            guide_urls.add(guide_url)
    return guide_urls


def get_guide_html_pages(guide_url, base_url):
    sitemap_url = urljoin(guide_url, "sitemap.xml")
    try:
        sitemap_doc = urlopen(sitemap_url, timeout=URL_TIMEOUT).read()
        soup = BeautifulSoup(sitemap_doc, "xml")
        return {loc.text.strip() for loc in soup.find_all("loc") if loc.text}
    except Exception:
        return set()


def get_guide_pdf_files(guide_url, base_url):
    pdf_url = get_guide_pdf(guide_url, base_url)
    return {pdf_url} if pdf_url else set()


def list_docs_files(start_page, get_guide_files):
    locale_path = "en_us/"
    base_url = "https://docs.aws.amazon.com"

    page = urlopen(start_page, timeout=URL_TIMEOUT)
    soup = BeautifulSoup(page, "xml")
    files = set()
    print("Generating file list (this may take some time)")

    service_links = soup.find_all("service")
    if not service_links:
        service_links = soup.find_all("list-card-item")
    print("Found " + str(len(service_links)) + " documentation entries")

    for link in service_links:
        uri = link.get("href")
        if not uri:
            continue
        print("URI: ", uri)
        service_url = urljoin(base_url, uri.split("?")[0])
        if urlparse(service_url).netloc != "docs.aws.amazon.com":
            print("Skipping external URL: " + service_url)
            continue

        # Current docs pages expose PDFs/HTML through each guide's metadata.
        files.update(get_guide_files(service_url, base_url))

        try:
            for guide_url in list_guide_urls(service_url, base_url):
                files.update(get_guide_files(guide_url, base_url))
        except Exception as exc:
            # Older docs landing pages used XML tiles instead of HTML guide links.
            try:
                if not uri.startswith("http"):
                    url = (
                        base_url + uri.split("?")[0] + locale_path + "landing-page.xml"
                    )
                else:
                    url = uri.split("?")[0]
                sub_page_doc = urlopen(url, timeout=URL_TIMEOUT)
                soup_doc = BeautifulSoup(sub_page_doc, "xml")
                for sublink in soup_doc.find_all("tile"):
                    try:
                        sub_url = sublink.get("href")
                        directory = base_url + "/".join(
                            urlsplit(sub_url).path.split("/")[:-1]
                        )
                        files.update(get_guide_files(directory + "/", base_url))
                    except Exception:
                        continue
            except Exception:
                print("Skipping " + service_url + " - " + str(exc))
                continue
    print("Found " + str(len(files)) + " documentation files")
    return files


def list_docs_pdfs(start_page):
    return list_docs_files(start_page, get_guide_pdf_files)


def list_docs_html(start_page):
    return list_docs_files(start_page, get_guide_html_pages)


def save_pdf(full_dir, filename, i, force):
    if not os.path.exists(full_dir):
        os.makedirs(full_dir)
    # Open the URL and retrieve data
    file_loc = full_dir + filename
    if not os.path.exists(file_loc) or force:
        if i.startswith("//"):
            i = "http:" + i
        print("Downloading : " + i)
        with urlopen(i, timeout=URL_TIMEOUT) as web:
            print("Saving to : " + file_loc)
            # Save Data to disk
            with open(file_loc, "wb") as output:
                output.write(web.read())
    else:
        print(
            "Skipping "
            + i
            + " - file exists or is a dated API document, use './getAWSdocs.py --force' to force override"
        )


def html_filename(url):
    """Map a documentation page URL onto a local .html filename."""
    filename = urlsplit(url).path.split("/")[-1]
    if not filename:
        # Directory-style URL, e.g. .../userguide/
        return "index.html"
    if "." not in filename:
        return filename + ".html"
    return filename


def get_pdfs(pdf_list, force, html=False):
    for i in pdf_list:
        doc = i.split("/")
        doc_location = doc[3]
        if html:
            filename = html_filename(i)
        else:
            filename = urlsplit(i).path.split("/")[-1]
        # Set download dir for whitepapers
        if "whitepapers" in doc_location:
            full_dir = "whitepapers/"
        else:
            # Set download dir and sub directories for documentation
            full_dir = "documentation/html/" if html else "documentation/"
            # Trailing "" for directory-style URLs is dropped along with the
            # filename, so the full path becomes the directory.
            directory = urlsplit(i).path.split("/")[:-1]
            for path in directory:
                if path != "":
                    full_dir = full_dir + path + "/"
        try:
            save_pdf(full_dir, filename, i, force)
        except Exception:
            continue


def main():
    args = get_options()
    # allow user to overwrite files
    force = args["force"]
    pdf_list = set()
    if args["documentation"]:
        print("Downloading Docs")
        landing_page = "https://docs.aws.amazon.com/en_us/main-landing-page.xml"
        is_html = args["documentation"] == "html"
        if is_html:
            docs_file_list = list_docs_html(landing_page)
        else:
            docs_file_list = list_docs_pdfs(landing_page)
        pdf_list.update(docs_file_list)
        get_pdfs(docs_file_list, force, html=is_html)

    if args["whitepapers"]:
        print("Downloading Whitepapaers")
        whitepaper_pdf_list = list_whitepaper_pdfs("http://aws.amazon.com/whitepapers/")
        pdf_list.update(whitepaper_pdf_list)
        get_pdfs(whitepaper_pdf_list, force)
        print("Downloading SAP Whitepapaers")
        sap_pdf_list = list_whitepaper_pdfs("https://aws.amazon.com/sap/whitepapers/")
        pdf_list.update(sap_pdf_list)
        get_pdfs(sap_pdf_list, force)

    for p in pdf_list:
        print(p)


if __name__ == "__main__":
    main()

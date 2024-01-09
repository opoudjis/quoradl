from requests_html import HTMLSession, AsyncHTMLSession
from bs4 import BeautifulSoup
import json
import logging
import re
from datetime import datetime
import random
import time
import argparse
import sys
import os
import requests
from email.mime.image import MIMEImage
from mimetypes import guess_type


logger = logging.getLogger("quora")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.DEBUG)


def markdownify(span, folder):
    """
    Given a "span" element, returns a segment of markdown text

    Supports __bold__, _italic_, ![](image) and [link](url) elements.
    Bold and italic elements are trimmed to avoid markdown parse errors.
    """

    raw_text = span.get("text", "")
    modifiers = span.get("modifiers", {})

    if not raw_text and not modifiers:
        return ""

    if modifiers.get("image"):
        return process_image(modifiers.get("image"), folder)

    bold = "__" if modifiers.get("bold") else ""
    italic = "_" if modifiers.get("italic") else ""
    target = ""
    islink = ("", "")
    postfix = ""

    if bold or italic:
        # we need to trim the text to accound for markdown, so
        # fixup danging spaces...
        old_len = len(raw_text)
        raw_text = raw_text.strip()
        postfix = " "

    if modifiers.get("link"):
        islink = "[", "]"
        target = f"({modifiers['link']['url']})"

    if modifiers.get("embed"):
        islink = "[", "]"
        if not raw_text and modifiers['embed'].get('title'):
            raw_text = f"{modifiers['embed'].get('title')}"
        target = f"({modifiers['embed']['url']})"

    text = (
        f"{bold}{italic}{islink[0]}{raw_text}{islink[1]}{target}{italic}{bold}{postfix}"
    )
    return text

def process_image(img_url, folder):
        img_basename = img_url.split("/")[-1]
        filename = img_basename
        if folder:
            filename = os.path.join(folder, filename)
        with open(filename, 'wb') as file:
            response = requests.get(img_url)
            file.write(response.content)
        (mimetype, encoding) = guess_type(filename)
        if mimetype:
            (maintype, subtype) = mimetype.split('/');
        else:
            subtype = "jpg"
        img_basename = img_basename + "." + subtype
        newname = img_basename
        if folder:
            newname = os.path.join(folder, newname)
        os.rename(filename, newname)
        return f"![]({img_basename})"

def recurse_expand_json(js):
    # some of the structures inside the quora json are escaped string jsons,
    # not actual json subunits... recursively inflate them
    for k, v in js.items():
        if isinstance(v, str) and v and  v[0] in ("[{"):
            js[k] = json.loads(v)
        if isinstance(js[k], dict):
            recurse_expand_json(js[k])


def get_quora_answer_data(URL):
    """
    Fetch a quora URL and parse out the answer data stashed in the window javascript

    Recursively expand the "data" section of the parsed script (which is not always
    stored as a proper nested json blcb), returning it as a proper nested dictionary.
    """
    session = HTMLSession()
    htmlrequest = session.get(URL)
    data_script = None

    # the data is stashed in an anonymous javascript tag which shares a bunch of
    # bollerplate with other scripts.  This seems to find only the correct script
    for each_script in htmlrequest.html.find("script"):
        if (
            "window.ansFrontendGlobals" in each_script.text
            and "creationTime" in each_script.text
        ):
            data_script = each_script.text
            break

    if not data_script:
        logger.warning("unable to find answer json")
        return

    # the data starts with a long  assignment to a some kind of hash; it's the first
    # assignment of it's type in the script, so we use it as landmark
    data_start = re.search(
        'window.ansFrontendGlobals.data.inlineQueryResults.results\["\S*"].push\(',
        data_script,
    ).span()[-1]

    # window.ansFrontendGlobals.data.inlineQueryResults.next is the next command, it's terminus
    for d in re.finditer(
        "window.ansFrontendGlobals.data.inlineQueryResults.next", data_script
    ):
        data_end, _ = d.span()
        if data_end > data_start:
            data_end -= 2
            break

    raw_answer_json = data_script[data_start:data_end]
    if not raw_answer_json:
        logger.warning("could not find end of answer json")
        return

    try:
        raw_answer_json = json.loads(raw_answer_json)
    except:
        logger.warning("json encoded data failed to parse")
        logger.warning(raw_answer_json)
        return

    # this is deliberate! The first invocation returns a
    # _string_, the second one gives us a json blob!
    raw_answer_json = json.loads(raw_answer_json)
    qdata = raw_answer_json["data"]
    recurse_expand_json(qdata)
    return qdata


def save_quora_answer(URL, filename=None, folder=None, force_lower=True):
    """
    saves answer in <URL> to <filename> or to a file in the local
    directory using a truncated version of the question text as
    a name.

    if force_lower is True (the default) the filename will be lowercased.
    Github links are case sensitive so forcing them to lower is the
    cheap cross-platform solution to link breakage.

    Return True if successfully written, or False if not
    """
    # trim to a legit filename
    if not filename:
        file_name_segments = URL.split("/")
        if "answer" in  file_name_segments:
            answer_tag = file_name_segments.index("answer")
        else:
            answer_tag = 0
        file_name_base = file_name_segments[answer_tag - 1]
        # some question titles are super long,
        # so truncate them for windows
        truncated = file_name_base[:230]
        if len(truncated) < len(file_name_base):
            truncated += str(hash(file_name_base))
        filename = truncated.encode("ascii", "ignore").decode("ascii")
        if force_lower:
            filename = filename.lower()

    # we'll usually be dealing with relative URLs from a list...
    if not URL.startswith("https://"):
        URL = "https://quora.com" + URL

    if not filename.lower().endswith(".md"):
        filename += ".md"

    logger.info(f"url:{URL}\n --> {filename}\n")
    qdata = get_quora_answer_data(URL)

    if not qdata:
        logger.warning("no file written")
        return

    # if the question has been deleted, the download will fail because the
    # downloader isn't logged in as you.. so skip:

    if "answer" in qdata:
        is_deleted = qdata["answer"]["question"].get("isDeleted")
        if is_deleted:
            logger.warning(f"could not process {URL}, the question was deleted")
            return False

    if "answer" in qdata:
        container = qdata["answer"]
    else:
        container = qdata["tribeItem"]["post"]
    
    answer_content = container["content"]
    found_text = False
    for section in answer_content["sections"]:
        for span in section["spans"]:
            if span["text"]:
                found_text = True
    if not found_text:
            logger.warning(f"could not process {URL}, the post is empty")
            return False

    # looks like 'updatedTime' is a different encoding??
    date_time_int = container["creationTime"]
    date_time_int /= 1000000
    written_date = datetime.fromtimestamp(date_time_int).date().strftime("%Y-%m-%d")

    space = "answer"
    if file_name_segments[0] == "https:":
        domain = file_name_segments[2].split(".")
        if domain[0] != "www":
            space = domain[0]

    filename = written_date + "-" + space + "-" + filename

    if folder:
        filename = os.path.join(folder, filename)

    # write out the MD file
    with open(f"{filename}", "w", encoding="utf-8") as out_file:


            # lazy way wrangle the json payload
    

        if "answer" in qdata:
            title_block = qdata["answer"]["question"]["title"]
        else:
            title_block = qdata["tribeItem"]["post"]["title"]
            if not title_block["sections"][0]["spans"][0]["text"]:
                title_block = qdata["tribeItem"]["post"]["contentQtextDocument"]["contentEmbedSection"]["content"].get("title")
            if not title_block or not title_block["sections"][0]["spans"][0]["text"]:
                title_block = qdata["tribeItem"]["post"]["contentQtextDocument"]["contentEmbedSection"]["content"]["question"]["title"]
        title_section = title_block["sections"][0]
        title = title_section["spans"][0]["text"]
        out_file.write(f"# {title}\n\n")
    
            # front matter
    
        author = container["author"]["names"][0]
        fname = author["familyName"]
        gname = author["givenName"]
        if author["reverseOrder"]:
            fname, gname = gname, fname
    
        out_file.write(f"\tauthor: {gname} {fname}\n")
    
        # looks like 'updatedTime' is a different encoding??
        date_time_int = container["creationTime"]
        date_time_int /= 1000000
        out_file.write(f"\twritten: {written_date}\n")

        views = container["numViews"]
        votes = container["numUpvotes"]
        out_file.write(f"\tviews: {views}\n")
        out_file.write(f"\tupvotes: {votes}\n")
   
  
        out_file.write(f"\tquora url: {URL}\n")
        question_url = container["url"]
        if question_url != URL:
            out_file.write(f"\tquestion url: {question_url}\n")

        if "answer" not in qdata:
            embed = container["content"]["sections"][0]["spans"][0]["modifiers"].get("embed")
            if embed and embed.get("url"):
                embed_url = embed.get("url")
                out_file.write(f"\tembedded url: {embed_url}\n")
    
        profile_url = container["author"]["profileUrl"]
        out_file.write(f"\tauthor url: {profile_url}\n")
    
        disclaimer = container.get("disclaimer")
        if disclaimer:
            out_file.write("\tdisclaimer:{disclaimer}\n")
    
            #repro = qdata["answer"]["isNotForReproduction"]
            #if repro:
                #out_file.write("\t**NOT FOR REPRODUCTION**\n")


        out_file.write("\n\n")

        # end front matter

        answer_content = container["content"]
            

        last_was_code = False

        for section in answer_content["sections"]:
            is_code = section.get("type") == "code"
            if is_code:
                out_file.write("    ")
            elif last_was_code:
                out_file.write("\n")

            if section["quoted"]:
                out_file.write("> ")

            if section["type"] == "unordered-list":
                out_file.write(section["indent"] * "    " + "* ")

            if section["type"] == "ordered-list":
                out_file.write(section["indent"] * "    " + "1. ")

            if section["type"] == "horizontal-rule":
                out_file.write("___\n")

            for _ in range(section.get("indent")):
                out_file.write("\t")

            for span in section["spans"]:
                text = markdownify(span, folder)
                out_file.write(text)

            # "sections" correspond to paragaraphs, so we add a markdown-friendly double
            out_file.write("\n")
            if not is_code:
                out_file.write("\n")
            last_was_code = is_code
    return True


def answers_from_quora_html(contentfile):
    """
    Unfortunately, getting an answer list is highly manual. This method has been tested with
    Chrome, should probably have analogues in other browsers.

    1) Go to `your content` (this won't work on _other_ people's content)
    2) Scroll down until you get to your first answer (they're sorted in reverse chronological order by default)
    3) Right click on a blank space in the window and choose "Inspect" or use Ctrl + Shift + I
    4) In the inspect pane which just opened, right click on the first <html> tag and choose Copy > Copy Element
    5) Paste the copied text into a utf-8 text file and save it
    6) Run this script with the name of the text file as an argument, ie

        python quoradl scrape my_answers_file.html

    The result will be a UTF-8 encoded text file where every line is the Quora URL of one of your
    answers, ie

    /What-is-Aristotle-1802/answer/Steve-Theodore


    """

    with open(contentfile, "r", encoding="utf-8") as htmlist:
        contents = htmlist.read()
        soup = BeautifulSoup(contents, "lxml")

        for link in soup.findAll("a", attrs={"href": re.compile("/answer/|(?<!www)(?<!help)\.quora\.com/.")}):
            yield link.get("href")


def save_answers_from_quora_html(contentfile, filename="quora_answers.txt"):
    """
    Save the parsed list of answers from <contentfile>
    """
    with open(filename, "wt", encoding="utf-8") as savefile:
        for link in answers_from_quora_html(contentfile):
            savefile.writelines(link + "\n")


def scrape_answers(
    contentfile, delay_min=1, delay_max=3, start=0, end=10000, folder=None
):
    """
    Given a manually saved Quora content page (see "answers_from_quora_html()" for details),
    convert them all to markdown in the current folder using auto-generated names
    based on the question names.

    delay_min and delay_max, if supplied,  are a range of randomized delay times in seconds,
    intended to  help avoid triggering Quoras anti-scraper alarms.  You can set this as low
    as works for your use case

    Start and end, if supplied, limits the processing to items specified by those index
    numbers -- mostly useful if you want to work in batches or resume after an interruption

    if folder is provided, save files to that folder

    """
    results = {}
    counter = 0
    for link in answers_from_quora_html(contentfile):
        if counter >= start and counter <= end:
            logger.debug(link)
            results[link] = save_quora_answer(link, folder=folder)
            time.sleep(random.randrange(delay_min, delay_max))
        elif counter > end:
            break
        logger.debug(f"{counter}")
        counter += 1

    logger.info("Download complete:")
    for k, v in results.items():
        if v:
            logger.info(f"      {k}")
        else:
            logger.info(f"ERROR {k}")

    logger.info(f"Completed items {start}-{end}")


# ---------  cli

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="quora download hack")

    subparsers = parser.add_subparsers(
        title="commands",
        help="use these commands to download one or multiple answers",
        required=True,
        dest="cmd",
    )
    dl_parser = subparsers.add_parser("download", help="download one answer")
    dl_parser.add_argument(
        "URL",
        type=str,
        help="download the supplied answer URL (in relative or full format)",
    )
    dl_parser.add_argument(
        "--output",
        type=str,
        help="if provided, save downloaded file with this path",
        default="",
    )

    scrape_parser = subparsers.add_parser(
        "scrape", help="download multiple answers from an html index file"
    )
    scrape_parser.add_argument(
        "htmlfile",
        type=str,
        help="download all of the answers in this html file (see --how-to for details on obtaining the html file",
    )
    scrape_parser.add_argument(
        "--folder",
        type=str,
        help="if provided, save to the supplied folder",
        default="",
    )

    howto = subparsers.add_parser(
        "howto",
        help="display instructions on how to scrape your quora content",
    )

    args = parser.parse_args()
    if args.cmd == "howto":
        print(
            """
         Unfortunately, getting an answer list is highly manual. This method has been tested with
        Chrome, should probably have analogues in other browsers:

        1) Go to `your content` (this won't work on _other_ people's content). You can limit 
           the results using the topic or year links if desired.
        2) Scroll down until you get to the oldest answer in your selection. They're sorted in reverse 
           chronological order by default.
        3) Right click on a blank space in the window and choose "Inspect" or use Ctrl + Shift + I
        4) In the inspect pane which just opened, right click on the first <html> tag and choose  Copy > Copy Element
        5) Paste the copied text into a utf-8 text file and save it
        6) Run this script again and pass the name of the file you created using the '--scrape' flag:

                python quoradl.py --scrape my_copied_answers.html 

        7) You can optionally provide the name of a folder to house the downloaded content using the "--folder" flag        
        """
        )
        sys.exit(0)

    if args.cmd == "download":
        filename = args.output
        URL = args.URL
        save_quora_answer(URL, filename)
        sys.exit(0)

    if not os.path.exists(args.htmlfile):
        print(f"could not find html file {htmlfile}")
        sys.exit(-1)

    if args.folder and not os.path.exists(args.folder):
        os.makedirs(args.folder, exist_ok=True)

    scrape_answers(args.htmlfile, folder=args.folder)


"""
Copyright 2021 Steve Theodore 

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

Enhancements by Nick Nicholas, 2024
"""

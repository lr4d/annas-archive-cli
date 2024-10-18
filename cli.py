#!/usr/bin/env -S conda run -n annas python
import argparse
import tempfile
import os
import requests as re
from lxml import html
from tqdm import tqdm
import libtorrent as lt
from bs4 import BeautifulSoup
import asyncio
from collections import deque

# Define your XPath definitions
book_xpaths = {
    "collection": "/html/body/main/div[3]/ul/li[last()]/div/a[1]",
    "torrent": "/html/body/main/div[3]/ul/li[last()]/div/a[2]/text()",
    "torrent_url": "/html/body/main/div[3]/ul/li[last()]/div/a[2]/@href",
    "filename_within_torrent": "/html/body/main/div[3]/ul/li[last()]/div/text()[3]",
    "title": "/html/body/main/div[1]/div[3]/text()",
    "extension": "/html/body/main/div[1]/div[2]/text()"
}

replace_chars = str.maketrans(dict.fromkeys(' /:', '.'))
state_str = ['queued', 'checking', 'downloading metadata', 'downloading', 'finished', 'seeding', 'allocating', 'checking fastresume']

download_queue = deque()

def file_search(torrent_info, desired_file):
    priorities = []
    fidx = -1
    size = -1
    path = ""
    for idx, des in enumerate(torrent_info.files()):
        if des.path.endswith(desired_file):
            priorities.append(255)
            fidx = idx
            size = des.size
            path = des.path
        else:
            priorities.append(0)
    if fidx == -1:
        raise Exception("Destination file not found in torrent")
    return (fidx, size, path, priorities)

def check_torrent_completion(ses, idx):
    alerts = ses.pop_alerts()
    for a in alerts:
        alert_type = type(a).__name__
        if alert_type == "file_completed_alert":
            if a.index == idx:
                return True
    return False

async def download_torrent(t_path, desired_file, save_filename, save_path="."):
    info = lt.torrent_info(t_path)
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})

    idx, size, path, priorities = file_search(info, desired_file)
    h = ses.add_torrent({'ti': info, 'save_path': save_path})
    h.prioritize_files(priorities)

    alert_mask = (lt.alert.category_t.error_notification |
                  lt.alert.category_t.performance_warning |
                  lt.alert.category_t.progress_notification)
    ses.set_alert_mask(alert_mask)
    old = 0  # hacky workaround for missing tqdm feature
    with tqdm(unit='B', unit_scale=True, unit_divisor=1024, miniters=1, total=size) as pbar:
        while True:
            s = h.status()
            prog = h.file_progress()[idx]
            add = prog - old
            old = prog
            pbar.update(add)
            pbar.set_description(f"{state_str[s.state]} ({s.num_peers} {'peer' if s.num_peers == 1 else 'peers'})", refresh=True)
            await asyncio.sleep(0.1)
            if check_torrent_completion(ses, idx):
                final_file_path = os.path.join(save_path, save_filename)
                os.rename(os.path.join(save_path, path), final_file_path)  # Rename to final file name
                pbar.update(size)
                pbar.set_description_str("finished", refresh=True)

                # Check file size and upload if greater than 30MB
                if size > 30 * 1024 * 1024:  # 30MB in bytes
                    upload_link = await upload_to_catbox(final_file_path)
                    return upload_link  # Return the upload link instead of local file path
                
                return final_file_path  # Return the local file path if not uploaded

def get_torrent_from_listing(url, use_hash_as_filename, guess_extension):
    page = re.get(url)
    tree = html.fromstring(page.content)

    fname = tree.xpath(book_xpaths["filename_within_torrent"])[0].split('“', 1)[1][:-1]
    t_url = tree.xpath(book_xpaths["torrent_url"])[0]
    torrent = tree.xpath(book_xpaths["torrent"])[0][:-1][1:]

    extension = tree.xpath(book_xpaths["extension"])[0].split(', ')[1]
    title = tree.xpath(book_xpaths["title"])[0].translate(replace_chars)

    save_as = fname if use_hash_as_filename else title
    if guess_extension:
        save_as += extension
    aa = url.split("/")
    return (f"{aa[0]}//{aa[2]}{str(t_url)}", torrent, fname, save_as)

def dl_torrent_from_listing(url, save_path=".", use_hash_as_filename=False, guess_extension=True):
    t_url, torrent, fname, save_as = get_torrent_from_listing(url, use_hash_as_filename, guess_extension)
    t = re.get(t_url, allow_redirects=True, stream=True)
    path = os.path.join(save_path, torrent)

    with open(path, "wb") as fout:
        with tqdm(unit='B', unit_scale=True, unit_divisor=1024, miniters=1,
                  desc=f"downloading {torrent}", total=int(t.headers.get('content-length', 0))) as pbar:
            for chunk in t.iter_content(chunk_size=4096):
                fout.write(chunk)
                pbar.update(len(chunk))
            pbar.update(int(t.headers.get('content-length', 0)))
            pbar.set_description_str(f"downloaded {torrent}", refresh=True)

    return (path, fname, save_as)

async def upload_to_catbox(file_path):
    with open(file_path, 'rb') as f:
        response = re.post('https://catbox.moe/user/api.php', data={'fileToUpload': f})
    response_data = response.json()
    
    if response_data.get('success'):
        return response_data.get('url')
    else:
        raise Exception("Failed to upload file to catbox.moe")

async def main():
    parser = argparse.ArgumentParser(description="Download torrents from Anna's Archive")
    subparsers = parser.add_subparsers(dest='command')

    # Search command
    search_parser = subparsers.add_parser('search', help='Search for books on Anna\'s Archive')
    search_parser.add_argument('term', type=str, help='Search term')

    # Download command
    download_parser = subparsers.add_parser('download', help='Download a torrent from a given URL')
    download_parser.add_argument('url', type=str, help='URL of the torrent listing')

    args = parser.parse_args()

    if args.command == 'search':
        await search(args.term)
    elif args.command == 'download':
        await download(args.url)
    else:
        parser.print_help()

async def search(term):
    url = f'https://annas-archive.org/search?q={term}'
    response = re.get(url)

    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'html.parser')
        book_entries = soup.find_all('div', class_='h-[125px] flex flex-col justify-center')

        results = []
        for entry in book_entries[:5]:  # Limit to the first 5 results
            title = entry.find('h3').get_text(strip=True)
            author = entry.find('div', class_='max-lg:line-clamp-[2] lg:truncate leading-[1.2] lg:leading-[1.35] max-lg:text-sm italic').get_text(strip=True)
            link = entry.a['href']  # Store the link
            filename = entry.find('div', class_='line-clamp-[2] leading-[1.2] text-[10px] lg:text-xs text-gray-500').get_text(strip=True)

            results.append((title, author, link, filename))  # Store all four values

        if results:
            print(f"Search Results for '{term}':")
            for i, (title, author, link, filename) in enumerate(results):
                full_link = f"https://annas-archive.org{link}"  # Construct the full URL
                print(f"{i+1}. {title} by {author} - {full_link}")

            # Ask the user to select a result
            selection = input("Select a result to download (1-5): ")
            if selection.isdigit() and 1 <= int(selection) <= len(results):
                selected_result = results[int(selection) - 1]
                await download(f"https://annas-archive.org{selected_result[2]}")
            else:
                print("Invalid selection. Please select a number between 1 and 5.")
        else:
            print('No results found.')
    else:
        print('Failed to fetch results. Please try again later.')

async def download(url):
    save_path = "./"
    
    try:
        path, fname, save_as = dl_torrent_from_listing(url, use_hash_as_filename=True, guess_extension=True)
        await download_torrent(path, fname, save_as, save_path=save_path)

        print(f"Download Complete! Your file is saved as {save_as}.")
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")

if __name__ == "__main__":
    asyncio.run(main())  # Use asyncio.run()

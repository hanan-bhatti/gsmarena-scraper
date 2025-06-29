import os
import json
import requests
from bs4 import BeautifulSoup
from time import sleep
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from datetime import datetime
import hashlib
from PIL import Image
import io
import webbrowser

# Constants
BASE_URL = "https://www.gsmarena.com/"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "gsmarena_data_json")
IMG_DIR = os.path.join(OUTPUT_DIR, "images")
TRACKING_DIR = os.path.join(OUTPUT_DIR, "tracking")

# Create directories if they don't exist
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(TRACKING_DIR, exist_ok=True)

# HTTP headers for requests
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Flask app setup
app = Flask(__name__)
app.config['SECRET_KEY'] = 'gsmarena_scraper_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

class GSMArenaScraperWeb:
    def __init__(self):
        self.is_scraping = False
        self.current_data = []
        self.brands = set()
        self.consecutive_errors = 0
        self.max_consecutive_errors = 5
        self.load_existing_data()

    def log_message(self, message):
        """Log messages to both the console and the frontend via SocketIO"""
        socketio.emit('log_message', {'message': message, 'timestamp': datetime.now().isoformat()})
        print(message)

    def update_progress(self, message):
        """Update scraping progress on the frontend"""
        socketio.emit('progress_update', {'message': message, 'timestamp': datetime.now().isoformat()})

    def generate_device_id(self, phone_name, phone_link):
        """Generate a unique ID for each device based on name and link"""
        unique_string = f"{phone_name}_{phone_link}"
        return hashlib.md5(unique_string.encode()).hexdigest()[:12]

    def load_scraped_devices_tracking(self, brand_name):
        """Load tracking file for a brand to see which devices were already scraped"""
        tracking_file = os.path.join(TRACKING_DIR, f"{brand_name.replace(' ', '_')}_tracking.json")
        if os.path.exists(tracking_file):
            try:
                with open(tracking_file, 'r', encoding='utf-8') as f:
                    tracking_data = json.load(f)
                    return tracking_data.get('scraped_devices', {}), tracking_data.get('last_updated', None)
            except Exception as e:
                self.log_message(f"Error loading tracking file for {brand_name}: {e}")
        return {}, None

    def save_scraped_devices_tracking(self, brand_name, scraped_devices):
        """Save tracking file for a brand with scraped device IDs"""
        tracking_file = os.path.join(TRACKING_DIR, f"{brand_name.replace(' ', '_')}_tracking.json")
        tracking_data = {
            'brand_name': brand_name,
            'scraped_devices': scraped_devices,
            'last_updated': datetime.now().isoformat(),
            'total_scraped': len(scraped_devices)
        }
        try:
            with open(tracking_file, 'w', encoding='utf-8') as f:
                json.dump(tracking_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log_message(f"Error saving tracking file for {brand_name}: {e}")

    def send_tracking_logs_to_frontend(self):
        """Send all tracking information to the frontend"""
        tracking_summary = {}
        total_scraped = 0

        if os.path.exists(TRACKING_DIR):
            for filename in os.listdir(TRACKING_DIR):
                if filename.endswith('_tracking.json'):
                    try:
                        filepath = os.path.join(TRACKING_DIR, filename)
                        with open(filepath, 'r', encoding='utf-8') as f:
                            tracking_data = json.load(f)
                            brand_name = tracking_data.get('brand_name', filename.replace('_tracking.json', '').replace('_', ' '))
                            scraped_count = tracking_data.get('total_scraped', 0)
                            last_updated = tracking_data.get('last_updated', 'Unknown')

                            tracking_summary[brand_name] = {
                                'scraped_count': scraped_count,
                                'last_updated': last_updated,
                                'devices': list(tracking_data.get('scraped_devices', {}).keys())
                            }
                            total_scraped += scraped_count
                    except Exception as e:
                        self.log_message(f"Error reading tracking file {filename}: {e}")

        socketio.emit('tracking_summary', {
            'brands': tracking_summary,
            'total_scraped': total_scraped,
            'total_brands': len(tracking_summary)
        })
        return tracking_summary

    def verify_device_image_exists(self, phone_data):
        """Verify if the device image exists locally and is valid"""
        if not phone_data.get('image_local'):
            return False

        image_path = os.path.join(OUTPUT_DIR, phone_data['image_local'])
        if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
            return False

        try:
            with Image.open(image_path) as img:
                img.verify()
            return True
        except Exception as e:
            self.log_message(f"    ⚠️ Invalid image: {image_path} - {e}")
            return False

    def verify_and_update_tracking_with_images(self):
        """Verify all tracked devices have their images and update tracking accordingly"""
        self.log_message("🔍 Verifying images for all tracked devices...")

        total_verified = 0
        total_missing_images = 0
        brands_updated = []

        if not os.path.exists(TRACKING_DIR):
            self.log_message("No tracking directory found")
            return

        for filename in os.listdir(TRACKING_DIR):
            if not filename.endswith('_tracking.json'):
                continue

            try:
                brand_name = filename.replace('_tracking.json', '').replace('_', ' ')
                tracking_file = os.path.join(TRACKING_DIR, filename)

                with open(tracking_file, 'r', encoding='utf-8') as f:
                    tracking_data = json.load(f)

                scraped_devices = tracking_data.get('scraped_devices', {})
                devices_to_remove = []

                brand_file = os.path.join(OUTPUT_DIR, f"{brand_name.replace(' ', '_')}.json")
                brand_phones = {}

                if os.path.exists(brand_file):
                    with open(brand_file, 'r', encoding='utf-8') as f:
                        brand_data = json.load(f)
                        if isinstance(brand_data, list):
                            brand_phones = {phone.get('device_id', self.generate_device_id(phone.get('name', ''), phone.get('link', ''))): phone for phone in brand_data}

                for device_id, device_info in scraped_devices.items():
                    total_verified += 1

                    phone_data = brand_phones.get(device_id)
                    if not phone_data:
                        self.log_message(f"  ⚠️ {brand_name}: Device {device_info.get('name', device_id)} not found in brand data")
                        continue

                    has_image = self.verify_device_image_exists(phone_data)

                    if not has_image:
                        total_missing_images += 1
                        devices_to_remove.append(device_id)
                        self.log_message(f"  ❌ {brand_name}: Missing or invalid image for {device_info.get('name', device_id)}")

                if devices_to_remove:
                    for device_id in devices_to_remove:
                        del scraped_devices[device_id]

                    tracking_data['scraped_devices'] = scraped_devices
                    tracking_data['total_scraped'] = len(scraped_devices)
                    tracking_data['last_verified'] = datetime.now().isoformat()

                    with open(tracking_file, 'w', encoding='utf-8') as f:
                        json.dump(tracking_data, f, indent=2, ensure_ascii=False)

                    brands_updated.append(brand_name)
                    self.log_message(f"  ✅ {brand_name}: Removed {len(devices_to_remove)} devices with missing or invalid images from tracking")

            except Exception as e:
                self.log_message(f"  ✖ Error verifying {filename}: {e}")

        self.log_message(f"📊 Image Verification Complete:")
        self.log_message(f"   Total devices verified: {total_verified}")
        self.log_message(f"   Missing or invalid images found: {total_missing_images}")
        self.log_message(f"   Brands updated: {len(brands_updated)}")

        if brands_updated:
            self.log_message(f"   Updated brands: {', '.join(brands_updated)}")

    def start_scraping(self, concurrent_downloads=10, delay=25):
        """Start the scraping process in a separate thread"""
        if self.is_scraping:
            return False

        self.is_scraping = True
        self.consecutive_errors = 0
        socketio.emit('scraping_started')

        thread = threading.Thread(target=self.scrape_data, args=(concurrent_downloads, delay), daemon=True)
        thread.start()
        return True

    def stop_scraping(self):
        """Stop the scraping process"""
        self.is_scraping = False
        socketio.emit('scraping_stopped')

    def get_soup_with_retry(self, url, max_retries=3, backoff_factor=2):
        """Fetch HTML content with retry mechanism and rate limiting"""
        for attempt in range(max_retries):
            try:
                if not self.is_scraping:
                    return None

                response = requests.get(url, headers=headers, timeout=30)

                if response.status_code == 429:
                    self.consecutive_errors += 1
                    retry_after = int(response.headers.get('Retry-After', 60))
                    self.log_message(f"⚠️ Rate limited. Waiting {retry_after} seconds... (Error {self.consecutive_errors}/{self.max_consecutive_errors})")

                    if self.consecutive_errors >= self.max_consecutive_errors:
                        self.log_message(f"❌ Too many consecutive rate limit errors ({self.consecutive_errors}). Stopping scraper.")
                        self.stop_scraping()
                        return None

                    sleep(retry_after)
                    continue

                if response.status_code != 200:
                    self.consecutive_errors += 1
                    self.log_message(f"⚠️ HTTP {response.status_code} error for {url}. Attempt {attempt + 1}/{max_retries}")

                    if self.consecutive_errors >= self.max_consecutive_errors:
                        self.log_message(f"❌ Too many consecutive HTTP errors ({self.consecutive_errors}). Stopping scraper.")
                        self.stop_scraping()
                        return None

                    if attempt < max_retries - 1:
                        sleep(backoff_factor ** attempt)
                    continue

                self.consecutive_errors = 0
                response.raise_for_status()
                return BeautifulSoup(response.text, 'html.parser')

            except requests.exceptions.RequestException as e:
                self.consecutive_errors += 1
                self.log_message(f"⚠️ Request error for {url}: {e}. Attempt {attempt + 1}/{max_retries}")

                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.log_message(f"❌ Too many consecutive request errors ({self.consecutive_errors}). Stopping scraper.")
                    self.stop_scraping()
                    return None

                if attempt < max_retries - 1:
                    sleep(backoff_factor ** attempt)

        self.log_message(f"❌ Failed to fetch {url} after {max_retries} attempts")
        return None

    def scrape_data(self, concurrent_downloads, delay):
        """Main scraping logic"""
        try:
            all_brands = self.scrape_brands()
            if not all_brands:
                self.log_message("❌ Failed to scrape brands list")
                return

            self.save_json("index.json", all_brands)
            self.log_message(f"Found {len(all_brands)} brands")

            for brand in all_brands:
                if not self.is_scraping:
                    break

                brand_name = brand['name']
                brand_file = f"{brand_name.replace(' ', '_')}.json"
                brand_file_path = os.path.join(OUTPUT_DIR, brand_file)

                existing_phones = {}
                scraped_devices_tracking, last_updated = self.load_scraped_devices_tracking(brand_name)

                if os.path.exists(brand_file_path):
                    try:
                        with open(brand_file_path, 'r', encoding='utf-8') as f:
                            existing_data = json.load(f)
                            if isinstance(existing_data, list):
                                existing_phones = {phone['name']: phone for phone in existing_data}
                        self.log_message(f"Found {len(existing_phones)} existing phones for {brand_name}")
                    except Exception as e:
                        self.log_message(f"Error loading existing data for {brand_name}: {e}")
                        existing_phones = {}

                self.log_message(f"🔍 Scraping brand: {brand_name}")
                self.log_message(f"📊 Tracking shows {len(scraped_devices_tracking)} previously scraped devices")
                self.update_progress(f"Scraping {brand_name}")

                try:
                    all_phones = self.scrape_phones(brand["link"])
                    if not all_phones:
                        self.log_message(f"⚠️ No phones found for {brand_name} or scraping stopped")
                        continue

                    self.log_message(f"Found {len(all_phones)} total phones for {brand_name}")

                    phones_to_scrape = []
                    already_scraped_count = 0

                    for phone in all_phones:
                        device_id = self.generate_device_id(phone['name'], phone['link'])
                        if device_id not in scraped_devices_tracking:
                            phone['device_id'] = device_id
                            phones_to_scrape.append(phone)
                        else:
                            already_scraped_count += 1

                    self.log_message(f"📱 {already_scraped_count} devices already scraped")
                    self.log_message(f"🆕 {len(phones_to_scrape)} new devices to scrape")

                    if phones_to_scrape:
                        success = self.scrape_phones_with_tracking(phones_to_scrape, existing_phones, brand_name, brand_file_path, scraped_devices_tracking, concurrent_downloads, delay)
                        if not success:
                            self.log_message(f"⚠️ Scraping stopped for {brand_name} due to errors")
                            break
                    else:
                        self.log_message(f"✅ {brand_name} is up to date with {len(existing_phones)} phones")

                except Exception as e:
                    self.log_message(f"✖ Error scraping brand {brand_name}: {e}")
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= self.max_consecutive_errors:
                        self.log_message(f"❌ Too many consecutive brand errors. Stopping scraper.")
                        break

            if self.is_scraping:
                self.log_message("🎉 Scraping completed!")
                self.update_progress("Completed")
            else:
                self.log_message("⚠️ Scraping stopped by user or due to errors")
                self.update_progress("Stopped")

        except Exception as e:
            self.log_message(f"Fatal error: {e}")
            self.update_progress("Error occurred")
        finally:
            self.is_scraping = False
            socketio.emit('scraping_finished')
            self.load_existing_data()

    def scrape_phones_with_tracking(self, phones_to_scrape, existing_phones, brand_name, brand_file_path, scraped_devices_tracking, max_workers, delay):
        """Scrape phones with tracking and retry mechanism"""
        all_phones_data = existing_phones.copy()
        completed_count = len(existing_phones)
        total_phones = len(phones_to_scrape) + completed_count
        current_scraped_tracking = scraped_devices_tracking.copy()
        failed_phones = []

        def scrape_and_save_single_phone(phone):
            if not self.is_scraping:
                return None
            try:
                self.log_message(f"  📱 Scraping: {phone['name']}")
                phone_data = self.scrape_phone_specs(phone)

                if phone_data:
                    phone_data['device_id'] = phone['device_id']
                    phone_data['scraped_timestamp'] = datetime.now().isoformat()

                    all_phones_data[phone_data['name']] = phone_data
                    current_scraped_tracking[phone['device_id']] = {
                        'name': phone_data['name'],
                        'scraped_at': phone_data['scraped_timestamp'],
                        'link': phone['link']
                    }

                    self.save_json(brand_name.replace(' ', '_') + '.json', list(all_phones_data.values()))
                    self.save_scraped_devices_tracking(brand_name, current_scraped_tracking)

                    phone_data['brand'] = brand_name
                    self.update_current_data_single_phone(phone_data, brand_name)

                    nonlocal completed_count
                    completed_count += 1
                    socketio.emit('phone_scraped', {
                        'phone': phone_data,
                        'brand': brand_name,
                        'device_id': phone['device_id'],
                        'progress': f"{completed_count}/{total_phones}",
                        'percentage': round((completed_count / total_phones) * 100, 1),
                        'scraped_count': len(current_scraped_tracking)
                    })

                    self.log_message(f"    ✅ Saved {phone['name']} (ID: {phone['device_id'][:8]}...) ({completed_count}/{total_phones})")
                    self.consecutive_errors = 0
                else:
                    self.log_message(f"    ⚠️ No data returned for {phone['name']}, adding to retry list")
                    failed_phones.append(phone)

                sleep(delay)
                return phone_data
            except Exception as e:
                self.log_message(f"    ✖ Failed to scrape {phone['name']}: {e}")
                failed_phones.append(phone)
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.log_message(f"❌ Too many consecutive errors. Stopping brand.")
                    self.is_scraping = False
                return None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_phone = {executor.submit(scrape_and_save_single_phone, phone): phone for phone in phones_to_scrape}
            for future in as_completed(future_to_phone):
                if not self.is_scraping:
                    self.log_message("⚠️ Scraping stopped, cancelling remaining tasks")
                    break
                try:
                    future.result()
                except Exception as e:
                    self.log_message(f"    ✖ Thread execution error: {e}")

        retry_attempts = 2
        for attempt in range(retry_attempts):
            if not failed_phones or not self.is_scraping:
                break
            self.log_message(f"🔄 Retrying {len(failed_phones)} failed phones (Attempt {attempt + 1}/{retry_attempts})")
            phones_to_retry = failed_phones.copy()
            failed_phones.clear()

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_phone = {executor.submit(scrape_and_save_single_phone, phone): phone for phone in phones_to_retry}
                for future in as_completed(future_to_phone):
                    if not self.is_scraping:
                        break
                    try:
                        future.result()
                    except Exception as e:
                        self.log_message(f"    ✖ Retry thread execution error: {e}")

        if self.is_scraping:
            self.log_message(f"✅ Completed {brand_name}: {len(all_phones_data)} total phones, {len(current_scraped_tracking)} tracked devices")
            if failed_phones:
                self.log_message(f"⚠️ {len(failed_phones)} devices still failed after retries")
            return True
        else:
            self.log_message(f"⚠️ Stopped {brand_name} scraping: {len(all_phones_data)} total phones, {len(current_scraped_tracking)} tracked devices")
            return False

    def update_current_data_single_phone(self, phone_data, brand_name):
        """Update current_data with a single phone immediately"""
        self.current_data = [p for p in self.current_data if p.get('device_id') != phone_data.get('device_id')]
        phone_data_copy = phone_data.copy()
        phone_data_copy['brand'] = brand_name
        self.current_data.append(phone_data_copy)
        self.brands.add(brand_name)
        socketio.emit('data_updated', {
            'total_phones': len(self.current_data),
            'total_brands': len(self.brands)
        })

    def save_json(self, filename, data):
        """Save data to a JSON file"""
        try:
            with open(os.path.join(OUTPUT_DIR, filename), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log_message(f"Error saving {filename}: {e}")

    def download_image(self, img_url, phone_name):
        """Download and verify an image"""
        if not img_url:
            return None

        img_ext = os.path.splitext(img_url)[-1].split("?")[0] or '.jpg'
        img_name = f"{phone_name.replace(' ', '_').replace('/', '-')}{img_ext}"
        img_path = os.path.join(IMG_DIR, img_name)

        if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
            try:
                with Image.open(img_path) as img:
                    img.verify()
                self.log_message(f"    ℹ️ Using existing valid image: {img_name}")
                return img_path
            except Exception as e:
                self.log_message(f"    ⚠️ Existing image invalid: {img_path} - {e}")
                try:
                    os.remove(img_path)
                except:
                    pass

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.log_message(f"    📥 Downloading image (attempt {attempt + 1}): {img_name}")
                img_response = requests.get(img_url, headers=headers, timeout=30)
                img_response.raise_for_status()

                content_type = img_response.headers.get('Content-Type', '')
                if not content_type.startswith('image/'):
                    raise Exception(f"Invalid content type: {content_type}")

                if len(img_response.content) < 100:
                    raise Exception("Image file too small, likely not a real image")

                try:
                    img = Image.open(io.BytesIO(img_response.content))
                    img.verify()
                except Exception as e:
                    raise Exception(f"Invalid image content: {e}")

                with open(img_path, 'wb') as handler:
                    handler.write(img_response.content)

                with Image.open(img_path) as img:
                    img.verify()

                self.log_message(f"    ✅ Image downloaded and verified: {img_name}")
                return img_path

            except Exception as e:
                self.log_message(f"    ⚠️ Image download attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    sleep(1)
                else:
                    self.log_message(f"    ❌ Failed to download image after {max_retries} attempts: {img_name}")
                    if os.path.exists(img_path):
                        try:
                            os.remove(img_path)
                        except:
                            pass
                    return None

    def scrape_brands(self):
        """Scrape the list of brands from GSMArena"""
        soup = self.get_soup_with_retry(BASE_URL + "makers.php3")
        if not soup:
            return []

        brands = soup.select(".st-text td a")
        brand_list = [{"name": a.text.strip(), "link": BASE_URL + a['href']} for a in brands]
        return brand_list

    def scrape_phones(self, brand_url):
        """Scrape all phones for a given brand"""
        phones = []
        current_url = brand_url
        page_num = 1

        self.log_message(f"  🔍 Starting pagination scraping for brand URL: {brand_url}")

        while current_url and self.is_scraping:
            try:
                self.log_message(f"  📄 Scraping page {page_num}: {current_url}")
                soup = self.get_soup_with_retry(current_url)

                if not soup:
                    self.log_message(f"    ❌ Failed to get soup for page {page_num}")
                    break

                phone_elements = soup.select(".makers ul li") or soup.select(".makers li") or soup.select("ul li")

                for li in phone_elements:
                    link_elem = li.find('a')
                    if link_elem and link_elem.get('href'):
                        phone_name = li.get_text(strip=True)
                        phone_link = BASE_URL + link_elem['href']

                        if '.php' in link_elem['href'] and phone_name:
                            phones.append({
                                "name": phone_name,
                                "link": phone_link
                            })

                self.log_message(f"    📱 Found {len(phones)} phones on page {page_num}")

                next_page = soup.select_one("a.pages-next") or soup.select_one(".nav-pages a.pages-next")
                if next_page and next_page.get('href'):
                    current_url = BASE_URL + next_page['href']
                    page_num += 1
                    sleep(1.0)
                else:
                    self.log_message(f"    🏁 No more pages found after page {page_num}")
                    current_url = None

            except Exception as e:
                self.log_message(f"    ✖ Error on page {page_num}: {e}")
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.log_message(f"❌ Too many consecutive page errors. Stopping brand scraping.")
                    break
                break

        self.log_message(f"  📱 Total phones found across {page_num} pages: {len(phones)}")
        return phones

    def scrape_phone_specs(self, phone):
        """Scrape detailed specifications for a phone"""
        soup = self.get_soup_with_retry(phone["link"])
        if not soup:
            return None

        try:
            title_elem = soup.select_one("h1.specs-phone-name-title")
            if not title_elem:
                self.log_message(f"    ⚠️ No title found for {phone['link']}")
                return None

            title = title_elem.text.strip()

            img_elem = soup.select_one("div.specs-photo-main img")
            img_url = img_elem["src"] if img_elem else ""
            local_img_path = None

            if img_url:
                local_img_path = self.download_image(img_url, title)
                if not local_img_path:
                    self.log_message(f"    ❌ Image download failed for {title}, marking device as incomplete")
                    return None
            else:
                self.log_message(f"    ℹ️ No image found for {title}")

            specs = {}
            for table in soup.select("div#specs-list table"):
                category_element = table.select_one("th")
                if not category_element:
                    continue
                category = category_element.text.strip()
                specs[category] = {}

                for tr in table.select("tr"):
                    if tr.select_one("th") and len(tr.select("td")) == 0:
                        continue

                    spec_name_elem = tr.select_one("td.ttl")
                    spec_value_elem = tr.select_one("td.nfo")

                    if spec_name_elem and spec_value_elem:
                        spec_name = spec_name_elem.text.strip()
                        spec_value = spec_value_elem.text.strip()
                        spec_value = ' '.join(spec_value.split())
                        if spec_value:
                            specs[category][spec_name] = spec_value
                    elif tr.select("td") and len(tr.select("td")) >= 2:
                        tds = tr.select("td")
                        if len(tds) >= 2:
                            spec_name = tds[0].text.strip()
                            spec_value = tds[1].text.strip()
                            spec_value = ' '.join(spec_value.split())
                            if spec_name and spec_value:
                                specs[category][spec_name] = spec_value

            return {
                "name": title,
                "image_url": img_url,
                "image_local": os.path.relpath(local_img_path, OUTPUT_DIR) if local_img_path else None,
                "link": phone["link"],
                "specs": specs
            }
        except Exception as e:
            self.log_message(f"    ✖ Error parsing specs for {phone['link']}: {e}")
            return None

    def load_existing_data(self):
        """Load existing scraped data into memory"""
        self.current_data = []
        self.brands = set()

        if os.path.exists(OUTPUT_DIR):
            for filename in os.listdir(OUTPUT_DIR):
                if filename.endswith('.json') and filename != 'index.json':
                    try:
                        filepath = os.path.join(OUTPUT_DIR, filename)
                        with open(filepath, 'r', encoding='utf-8') as f:
                            brand_data = json.load(f)
                            brand_name = filename.replace('.json', '').replace('_', ' ')
                            self.brands.add(brand_name)

                            if isinstance(brand_data, list):
                                for phone in brand_data:
                                    if isinstance(phone, dict):
                                        if 'device_id' not in phone:
                                            phone['device_id'] = self.generate_device_id(
                                                phone.get('name', ''),
                                                phone.get('link', '')
                                            )
                                        phone['brand'] = brand_name
                                        self.current_data.append(phone)
                    except Exception as e:
                        self.log_message(f"Error loading {filename}: {e}")

        socketio.emit('data_updated', {
            'total_phones': len(self.current_data),
            'total_brands': len(self.brands)
        })

# Initialize scraper
scraper = GSMArenaScraperWeb()

# Flask Routes
@app.route('/home')
def index():
    return render_template('index.html')

@app.route('/api/phones')
def get_phones():
    search = request.args.get('search', '').lower()
    brand = request.args.get('brand', '')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))

    all_data = scraper.current_data.copy()

    filtered_data = all_data

    if brand and brand != 'All':
        filtered_data = [p for p in filtered_data if p.get('brand', '').lower() == brand.lower()]

    if search:
        filtered_data = [p for p in filtered_data if search in p['name'].lower()]

    start = (page - 1) * per_page
    end = start + per_page
    paginated_data = filtered_data[start:end]

    return jsonify({
        'phones': paginated_data,
        'total': len(filtered_data),
        'page': page,
        'per_page': per_page,
        'has_next': end < len(filtered_data)
    })

@app.route('/api/brands')
def get_brands():
    return jsonify(sorted(list(scraper.brands)))

@app.route('/api/phone/<path:phone_name>')
def get_phone_details(phone_name):
    for phone in scraper.current_data:
        if phone['name'] == phone_name:
            return jsonify(phone)

    if os.path.exists(OUTPUT_DIR):
        for filename in os.listdir(OUTPUT_DIR):
            if filename.endswith('.json') and filename != 'index.json':
                try:
                    filepath = os.path.join(OUTPUT_DIR, filename)
                    with open(filepath, 'r', encoding='utf-8') as f:
                        brand_data = json.load(f)
                        if isinstance(brand_data, list):
                            for phone in brand_data:
                                if isinstance(phone, dict) and phone.get('name') == phone_name:
                                    phone['brand'] = filename.replace('.json', '').replace('_', ' ')
                                    return jsonify(phone)
                except Exception as e:
                    print(f"Error reading {filename}: {e}")
                    continue

    return jsonify({'error': 'Phone not found'}), 404

@app.route('/api/scrape/start', methods=['POST'])
def start_scraping():
    data = request.get_json() or {}
    concurrent = data.get('concurrent', 10)
    delay = data.get('delay', 25)

    if scraper.start_scraping(concurrent, delay):
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'message': 'Already scraping'})

@app.route('/api/scrape/stop', methods=['POST'])
def stop_scraping():
    scraper.stop_scraping()
    return jsonify({'success': True})

@app.route('/api/scrape/status')
def scrape_status():
    return jsonify({'is_scraping': scraper.is_scraping})

@app.route('/api/tracking/summary')
def get_tracking_summary():
    return jsonify(scraper.send_tracking_logs_to_frontend())

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMG_DIR, filename)

# SocketIO Events
@socketio.on('connect')
def handle_connect():
    emit('connected', {'data': 'Connected to GSMArena Scraper'})
    emit('data_updated', {
        'total_phones': len(scraper.current_data),
        'total_brands': len(scraper.brands)
    })
    scraper.send_tracking_logs_to_frontend()

@socketio.on('request_phone_update')
def handle_phone_update_request(data):
    phone_name = data.get('phone_name')
    if phone_name:
        for phone in scraper.current_data:
            if phone['name'] == phone_name:
                emit('phone_update', phone)
                return
        emit('phone_not_found', {'phone_name': phone_name})

@socketio.on('request_tracking_summary')
def handle_tracking_summary_request():
    scraper.send_tracking_logs_to_frontend()

def main():
    """Main function to start the scraper and web interface"""
    print("🚀 Starting GSMArena Scraper Web Interface...")
    print("📱 Open your browser and go to: http://localhost:9192/home")
    print("⚡ Features:")
    print("   - Modern web interface")
    print("   - Real-time scraping logs")
    print("   - Device tracking system")
    print("   - Individual device ID tracking")
    print("   - Resume interrupted scraping")
    print("   - Device search and filtering")
    print("   - Detailed device specifications")
    print("   - Image gallery")
    print("   - Responsive design")
    print("\n🔧 Settings:")
    print("   - Concurrent downloads: Adjustable (1-200)")
    print("   - Delay between requests: Adjustable (0.1-10s)")
    print("   - Auto-saves data in JSON format")
    print("   - Downloads device images locally")
    print("   - Tracks individual device scraping status")
    print("🚀 Starting GSMArena Scraper Web Interface...")

    print("🔍 Verifying existing device images...")
    scraper.verify_and_update_tracking_with_images()

    def open_browser():
        sleep(1.5)
        webbrowser.open('http://localhost:9192/home')

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    try:
        socketio.run(app, host='0.0.0.0', port=9192, debug=False)
    except KeyboardInterrupt:
        print("\n👋 Shutting down GSMArena Scraper...")

if __name__ == "__main__":
    main()
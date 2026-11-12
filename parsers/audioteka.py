"""Adapted from audioteka.com\\audioteka_parser.py; service-specific HTML parsing."""

import re
from bs4 import BeautifulSoup

def clean_at_cover(url):
    """Remove Audioteka CDN resizing suffixes and return the original asset URL."""
    if not url:
        return None
    return re.split(r"(?<=\.jpg)|(?<=\.jpeg)|(?<=\.png)|(?<=\.webp)", url, maxsplit=1, flags=re.IGNORECASE)[0].split("?")[0]

def parse_duration_to_minutes(text):
    """Convert a Polish duration string to minutes."""
    if not text: return None
    hours = 0
    minutes = 0
    
    h_match = re.search(r"(\d+)\s*godz", text)
    if h_match: hours = int(h_match.group(1))
        
    m_match = re.search(r"(\d+)\s*min", text)
    if m_match: minutes = int(m_match.group(1))
        
    total = (hours * 60) + minutes
    return total if total > 0 else None

def parse_audioteka_page(html, url):
    soup = BeautifulSoup(html, 'lxml')
    
    if "/audiobook/" in url:
        data = {
            "url": url, "title": None, "authors": [], "narrators": [], "publisher": [],
            "length_str": None, "duration_minutes": None, "type": None, "format": None, 
            "language": None, "category": None, "collections": [], "avg_rating": None, 
            "rating_count": None, "description": None, "why_worth_it": None, 
            "cover_url": None, "reviews": []
        }
        
        t_tag = soup.select_one('h1[class*="product-top_title"]')
        if t_tag: data["title"] = t_tag.get_text(strip=True)
        
        for a in soup.select('[class*="product-top_author"]'):
            data["authors"].append(a.get_text(strip=True))
            
        img = soup.select_one('img[class*="product-top_cover"]')
        if img: data["cover_url"] = clean_at_cover(img.get("src"))
        
        # Poprawiona ocena (odporna na puste stringi)
        rb = soup.select_one('[class*="rating-badge_badgeContent"]')
        if rb: 
            val_text = rb.get_text(strip=True).replace(',', '.')
            if val_text:
                try: data["avg_rating"] = float(val_text)
                except ValueError: pass
        
        # Poprawiona ilość głosów
        badge_box = soup.select_one('[class*="rating-badge_ratingBadge"]')
        if badge_box:
            badge_text = badge_box.get_text(" ", strip=True)
            match = re.search(r"z\s+(\d+)\s+ocen", badge_text.replace('\xa0', ' '))
            if match: data["rating_count"] = int(match.group(1))
        
        details = soup.select_one("#audiobook-more-info")
        if details:
            for dt, dd in zip(details.find_all('dt'), details.find_all('dd')):
                lab = dt.get_text(strip=True).lower()
                if "głosy" in lab: data["narrators"] = [x.get_text(strip=True) for x in dd.find_all('a')]
                elif "długość" in lab: 
                    data["length_str"] = dd.get_text(strip=True)
                    data["duration_minutes"] = parse_duration_to_minutes(data["length_str"])
                elif "wydawca" in lab: data["publisher"] = [x.get_text(strip=True) for x in dd.find_all('a')]
                elif "typ" in lab: data["type"] = dd.get_text(strip=True)
                elif "format" in lab: data["format"] = dd.get_text(strip=True)
                elif "język" in lab: data["language"] = dd.get_text(strip=True)
                elif "kategoria" in lab: 
                    cat_a = dd.find('a')
                    if cat_a: data["category"] = cat_a.get_text(strip=True)

        for c in soup.select('[class*="collections_list"] a'):
            data["collections"].append(c.get_text(strip=True))

        desc = soup.select_one("#audiobook-description")
        if desc: data["description"] = desc.get_text("\n", strip=True)
        
        why = soup.select_one('[class*="ai-review_reviewText"]')
        if why: data["why_worth_it"] = why.get_text("\n", strip=True)

        for li in soup.select('li[class*="review-item_reviewItem"]'):
            u = li.select_one('[class*="review-item_authorName"]')
            txt = li.select_one("[class*='review-item_review'] span[aria-hidden='true']")
            if u and txt:
                accessible = li.get_text(" ", strip=True)
                rating = re.search(r"ocena\s+(\d+(?:[.,]\d+)?)", accessible, re.I)
                date = li.select_one("time")
                date_match = re.search(r"\bz\s+(\d{1,2}\.\d{1,2}\.\d{4})", accessible)
                data["reviews"].append({
                    "user": u.get_text(strip=True), 
                    "text": txt.get_text(strip=True),
                    "date": (date.get("datetime") or date.get_text(strip=True)) if date else date_match[1] if date_match else None,
                    "stars": float(rating[1].replace(",", ".")) if rating else len(li.select('[class*="review-item_stars"] use[href$="#star"]')) or None
                })
        return {"type": "book", "data": data}

    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('/'): href = "https://audioteka.com" + href
        if "/pl/audiobook/" in href:
            links.append({"url": href.split('?')[0], "type": "book", "priority": 10})
        elif "/pl/katalog/" in href or "/pl/cykl/" in href:
            links.append({"url": href, "type": "list", "priority": 1})
    return {"type": "list", "links": links}
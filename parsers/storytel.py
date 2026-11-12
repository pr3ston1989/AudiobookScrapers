"""Adapted from storytel.com\\storytel_parser.py; service-specific HTML parsing."""

import re
import urllib.parse
from bs4 import BeautifulSoup

def clean_storytel_cover(url):
    if not url: return None
    return url.split('?')[0]

def parse_duration_to_minutes(text):
    if not text: return None
    hours, minutes = 0, 0
    h_match = re.search(r"(\d+)\s*godz", text, re.IGNORECASE)
    if h_match: hours = int(h_match.group(1))
    m_match = re.search(r"(\d+)\s*min", text, re.IGNORECASE)
    if m_match: minutes = int(m_match.group(1))
    total = (hours * 60) + minutes
    return total if total > 0 else None

def parse_storytel_page(html, url):
    soup = BeautifulSoup(html, 'lxml')
    data = {
        "url": url, "title": None, "authors": [], "narrators": [], "publisher": [],
        "length_str": None, "duration_minutes": None, "type": "audiobook", "format": None, 
        "language": None, "category": None, "collections": [], "avg_rating": None, 
        "rating_count": None, "description": None, "why_worth_it": None, 
        "cover_url": None, "reviews": [], "volume_number": None,
    }
    
    # 1. Podstawowe metadane
    t_tag = soup.select_one("h1[class*='styledTitle']")
    if t_tag: data["title"] = t_tag.get_text(strip=True)
    
    img = soup.select_one("img[class*='styledCover']")
    if img:
        if img.has_attr("src") and img["src"].startswith("http"):
            data["cover_url"] = clean_storytel_cover(img["src"])
        elif img.has_attr("srcset"):
            urls = re.findall(r'https?://[^\s,]+', img["srcset"])
            if urls: data["cover_url"] = clean_storytel_cover(urls[-1])
        
    for li in soup.select("ul[class*='contributorsList'] > li"):
        label = li.contents[0].strip().lower() if li.contents else ""
        links = [a.get_text(strip=True) for a in li.find_all("a")]
        if "autor" in label: data["authors"].extend(links)
        elif "z:" in label or "czyta" in label or "lektor" in label: data["narrators"].extend(links)
        elif "wydawca" in label: data["publisher"].extend(links)

    # 2. Tabela szczegółów (Tutaj są OCENY w Twoim HTML-u)
    dts = soup.select("dl dt")
    dds = soup.select("dl dd")
    for dt, dd in zip(dts, dds):
        label = dt.get_text(strip=True).lower()
        val_text = dd.get_text(" ", strip=True)
        
        if "ocen" in label:
            # Liczba ocen z <dt> (np. "10832 ocena" -> 10832)
            count_match = re.search(r"(\d+)", label.replace(" ", "").replace("\xa0", ""))
            if count_match: data["rating_count"] = int(count_match.group(1))
            
            # Średnia ocena zagnieżdżona w <dd> obok (np. "4.8")
            avg_p = dd.find('p')
            if avg_p:
                try: data["avg_rating"] = float(avg_p.get_text(strip=True).replace(',', '.'))
                except ValueError: pass
                
        elif "czas" in label:
            data["length_str"] = val_text
            data["duration_minutes"] = parse_duration_to_minutes(val_text)
        elif "język" in label: data["language"] = val_text
        elif "kategoria" in label: data["category"] = val_text
        elif "serie" in label or "seria" in label:
            series_link = dd.find('a')
            if series_link:
                raw_href = urllib.parse.unquote(series_link['href'].split('/')[-1])
                s_name = re.sub(r'-\d+$', '', raw_href).replace('-', ' ').title()
                s_vol = series_link.get_text(strip=True)
                
                # Czysta nazwa idzie do powiązanych kolekcji (np. "Wiedźmin")
                data["collections"].append(s_name)
                data["series"] = s_name
                # Numer tomu idzie do nowej, dedykowanej zmiennej (np. "1 z 7")
                position = re.match(r"\s*(\d+)\b", s_vol)
                data["volume_number"] = position[1] if position else s_vol
                data["series_position"] = s_vol

    for tag in soup.select("span[class*='chip_text']"):
        data["collections"].append(tag.get_text(strip=True))

    desc = soup.select_one("div[id*='expandable-text']")
    if desc: data["description"] = desc.get_text("\n", strip=True)
    
    # 3. Podsumowanie AI (Wyciągnięte bezbłędnie na podstawie Twojego HTML)
    why = soup.select_one("div[class*='summaryTextContainer'] p")
    if why: data["why_worth_it"] = why.get_text("\n", strip=True)

    # 4. Opinie użytkowników (Użyto luźnych klas re.compile dla odporności na zmiany)
    for card in soup.find_all("article", class_=re.compile("review-card")):
        user_elem = card.find("p", class_=re.compile("typography_strong"))
        text_elem = card.find(class_=re.compile("review-card_comment"))
        
        if user_elem and text_elem:
            user = user_elem.get_text(strip=True)
            text = text_elem.get_text(strip=True)
            
            # Data to zazwyczaj drugi paragraf pod imieniem
            date = ""
            parent_div = user_elem.parent
            if parent_div:
                ps = parent_div.find_all("p")
                if len(ps) > 1:
                    date = ps[1].get_text(strip=True)
            
            # Liczymy fizycznie szare gwiazdki SVG na kafelku użytkownika
            stars = len(card.select('svg[fill="#5c5c5cff"]'))
            
            data["reviews"].append({
                "user": user, 
                "date": date,
                "text": text, 
                "stars": stars
            })
            
    return {"type": "book", "data": data}
# ruff: noqa: E501 - one country per line reads better than wrapped rows
"""Countries, ports, parties and carriers for synthetic shipments.

Faker locales are used only where they produce Latin script; other countries get romanized
names from small word lists, so every PDF renders with standard fonts.
"""

import random
import string
from dataclasses import dataclass

from faker import Faker


@dataclass(frozen=True)
class Country:
    code: str  # ISO 3166-1 alpha-2
    name: str
    locale: str | None  # Faker locale with Latin script, else romanized names
    cities: tuple[str, ...]
    ports: tuple[str, ...]


# fmt: off
SELLERS = {
    c.code: c
    for c in (
        Country("CN", "China", None, ("Ningbo", "Shenzhen", "Guangzhou", "Xiamen", "Qingdao"), ("Shanghai", "Ningbo", "Shenzhen", "Qingdao")),
        Country("VN", "Vietnam", "vi_VN", ("Ho Chi Minh City", "Hai Phong", "Binh Duong"), ("Ho Chi Minh City", "Hai Phong")),
        Country("IN", "India", "en_IN", ("Mumbai", "Chennai", "Tiruppur", "Ludhiana"), ("Nhava Sheva", "Chennai", "Mundra")),
        Country("BD", "Bangladesh", None, ("Dhaka", "Gazipur", "Chattogram"), ("Chattogram",)),
        Country("TH", "Thailand", None, ("Bangkok", "Samut Prakan", "Chonburi"), ("Laem Chabang", "Bangkok")),
        Country("MY", "Malaysia", None, ("Kuala Lumpur", "Penang", "Johor Bahru"), ("Port Klang", "Penang")),
        Country("ID", "Indonesia", "id_ID", ("Jakarta", "Surabaya", "Semarang"), ("Tanjung Priok", "Tanjung Perak")),
        Country("TR", "Turkey", "tr_TR", ("Istanbul", "Izmir", "Bursa", "Denizli"), ("Ambarli", "Izmir")),
        Country("IT", "Italy", "it_IT", ("Milano", "Bologna", "Verona", "Prato"), ("Genova", "La Spezia")),
        Country("ES", "Spain", "es_ES", ("Valencia", "Jaén", "Barcelona"), ("Valencia", "Barcelona", "Algeciras")),
        Country("DE", "Germany", "de_DE", ("Stuttgart", "Köln", "Nürnberg"), ("Hamburg", "Bremerhaven")),
        Country("BR", "Brazil", "pt_BR", ("São Paulo", "Curitiba", "Santos"), ("Santos", "Paranaguá")),
        Country("KR", "South Korea", None, ("Busan", "Incheon", "Changwon"), ("Busan", "Incheon")),
        Country("TW", "Taiwan", None, ("Taichung", "Kaohsiung", "Tainan"), ("Kaohsiung", "Keelung")),
        Country("PK", "Pakistan", None, ("Karachi", "Lahore", "Faisalabad"), ("Karachi",)),
        Country("EC", "Ecuador", "es_ES", ("Guayaquil", "Manta"), ("Guayaquil",)),
        Country("CO", "Colombia", "es_CO", ("Medellín", "Bogotá", "Armenia"), ("Cartagena", "Buenaventura")),
    )
}

BUYERS = {
    c.code: c
    for c in (
        Country("US", "United States", "en_US", ("Los Angeles", "Newark", "Savannah"), ("Los Angeles", "New York", "Savannah")),
        Country("GB", "United Kingdom", "en_GB", ("Manchester", "Birmingham", "Leeds"), ("Felixstowe", "Southampton")),
        Country("DE", "Germany", "de_DE", ("Hamburg", "Frankfurt am Main", "Düsseldorf"), ("Hamburg",)),
        Country("NL", "Netherlands", "nl_NL", ("Rotterdam", "Utrecht", "Tilburg"), ("Rotterdam",)),
        Country("FR", "France", "fr_FR", ("Lyon", "Lille", "Paris"), ("Le Havre", "Marseille")),
        Country("BE", "Belgium", "nl_BE", ("Antwerpen", "Gent"), ("Antwerp",)),
        Country("ZA", "South Africa", "en_GB", ("Johannesburg", "Durban", "Cape Town"), ("Durban", "Cape Town")),
        Country("AU", "Australia", "en_AU", ("Sydney", "Melbourne", "Brisbane"), ("Sydney", "Melbourne")),
        Country("CA", "Canada", "en_CA", ("Toronto", "Vancouver", "Montréal"), ("Vancouver", "Montreal")),
        Country("PL", "Poland", "pl_PL", ("Warszawa", "Łódź", "Poznań"), ("Gdańsk",)),
    )
}
# fmt: on

_ROMANIZED = {
    "CN": ("Hengda", "Jinlong", "Huaxin", "Tianyi", "Xinda", "Ruifeng", "Yongsheng", "Haixing"),
    "BD": ("Rahman", "Karim", "Padma", "Meghna", "Sonali", "Rupali", "Bengal"),
    "TH": ("Siam", "Chao Phraya", "Thanakorn", "Sukjai", "Rattana", "Chaiyo"),
    "MY": ("Sinar", "Mutiara", "Perdana", "Cahaya", "Gemilang", "Bintang"),
    "KR": ("Hanil", "Daesung", "Samjin", "Kumho", "Seojin", "Hyopsung"),
    "TW": ("Formosa", "Chunghwa", "Tatung", "Yungshin", "Hsinchu", "Lienhwa"),
    "PK": ("Al-Karam", "Nishat", "Gul Ahmed", "Crescent", "Indus", "Sapphire"),
}
_TRADES = ("Industrial", "Trading", "Import & Export", "Manufacturing", "Textile", "Electronics")
_STREETS = ("Industrial Road", "Harbour Road", "Export Processing Zone", "Ring Road", "Avenue 3")

CARRIERS = (
    ("MSC", "MEDU"),
    ("MAERSK", "MAEU"),
    ("CMA CGM", "CMDU"),
    ("COSCO", "COSU"),
    ("HAPAG-LLOYD", "HLCU"),
    ("ONE", "ONEY"),
    ("EVERGREEN", "EGLV"),
)
VESSEL_NAMES = ("AURORA", "KENSINGTON", "TAGE", "PEGASUS", "VALENCIA", "HARMONY", "MERIDIAN", "ORION", "SERENITY", "TRITON", "ZEPHYR", "CALYPSO")  # fmt: skip


@dataclass(frozen=True)
class Party:
    name: str
    address: tuple[str, ...]
    country: str  # ISO 3166-1 alpha-2


def make_party(rng: random.Random, country: Country) -> Party:
    city = rng.choice(country.cities)
    if country.locale is not None:
        fake = Faker(country.locale)
        fake.seed_instance(rng.getrandbits(32))
        street = fake.street_address().replace("\n", ", ")
        address = (street, f"{fake.postcode()} {city}", country.name)
        return Party(fake.company(), address, country.code)
    word = rng.choice(_ROMANIZED[country.code])
    name = f"{city} {word} {rng.choice(_TRADES)} Co., Ltd."
    street = f"No. {rng.randint(1, 388)} {rng.choice(_STREETS)}"
    return Party(name, (street, city, country.name), country.code)


_LETTER_VALUES = [value for value in range(10, 39) if value % 11]  # ISO 6346 skips 11, 22, 33
_ISO6346 = dict(zip(string.digits, range(10), strict=True)) | dict(
    zip(string.ascii_uppercase, _LETTER_VALUES, strict=True)
)


def iso6346_check_digit(code: str) -> int:
    """Check digit for the first 10 characters of a container number (owner, category, serial)."""
    return sum(_ISO6346[ch] * 2**position for position, ch in enumerate(code)) % 11 % 10


def container_number(rng: random.Random, owner: str) -> str:
    """An ISO 6346 container number (owner code + U + serial) with a valid check digit."""
    code = f"{owner[:3]}U{rng.randint(0, 999_999):06d}"
    return f"{code}{iso6346_check_digit(code)}"

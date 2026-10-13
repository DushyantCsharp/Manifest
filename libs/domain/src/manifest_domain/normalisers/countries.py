"""Country names and codes to ISO 3166-1 alpha-2.

Documents print origins as codes ("CN"), names ("China", "SOUTH KOREA") or official forms
("Korea, Republic of"). The table holds every officially assigned alpha-2 code with its ISO
English short name and the common alternatives seen on trade documents. Matching ignores case,
accents, punctuation, a leading "the" and a "made in" prefix. A bare "Korea" or "Guinea"-like
prefix is not guessed: anything not in the table is a ``NormalisationError``.
"""

import re
import unicodedata

from manifest_domain.normalisers.numbers import NormalisationError

# code | ISO English short name | alternatives
_TABLE = """
AD|Andorra
AE|United Arab Emirates|UAE
AF|Afghanistan
AG|Antigua and Barbuda
AI|Anguilla
AL|Albania
AM|Armenia
AO|Angola
AQ|Antarctica
AR|Argentina
AS|American Samoa
AT|Austria
AU|Australia
AW|Aruba
AX|Åland Islands
AZ|Azerbaijan
BA|Bosnia and Herzegovina
BB|Barbados
BD|Bangladesh
BE|Belgium
BF|Burkina Faso
BG|Bulgaria
BH|Bahrain
BI|Burundi
BJ|Benin
BL|Saint Barthélemy
BM|Bermuda
BN|Brunei Darussalam|Brunei
BO|Bolivia, Plurinational State of|Bolivia
BQ|Bonaire, Sint Eustatius and Saba
BR|Brazil|Brasil
BS|Bahamas
BT|Bhutan
BV|Bouvet Island
BW|Botswana
BY|Belarus
BZ|Belize
CA|Canada
CC|Cocos (Keeling) Islands
CD|Congo, Democratic Republic of the|Democratic Republic of the Congo|DR Congo
CF|Central African Republic
CG|Congo|Republic of the Congo
CH|Switzerland
CI|Côte d'Ivoire|Ivory Coast
CK|Cook Islands
CL|Chile
CM|Cameroon
CN|China|People's Republic of China|PRC|P.R. China
CO|Colombia
CR|Costa Rica
CU|Cuba
CV|Cabo Verde|Cape Verde
CW|Curaçao
CX|Christmas Island
CY|Cyprus
CZ|Czechia|Czech Republic
DE|Germany|Deutschland
DJ|Djibouti
DK|Denmark
DM|Dominica
DO|Dominican Republic
DZ|Algeria
EC|Ecuador
EE|Estonia
EG|Egypt
EH|Western Sahara
ER|Eritrea
ES|Spain|España
ET|Ethiopia
FI|Finland
FJ|Fiji
FK|Falkland Islands (Malvinas)|Falkland Islands
FM|Micronesia, Federated States of|Micronesia
FO|Faroe Islands
FR|France
GA|Gabon
GB|United Kingdom of Great Britain and Northern Ireland|United Kingdom|UK|Great Britain
GD|Grenada
GE|Georgia
GF|French Guiana
GG|Guernsey
GH|Ghana
GI|Gibraltar
GL|Greenland
GM|Gambia
GN|Guinea
GP|Guadeloupe
GQ|Equatorial Guinea
GR|Greece
GS|South Georgia and the South Sandwich Islands
GT|Guatemala
GU|Guam
GW|Guinea-Bissau
GY|Guyana
HK|Hong Kong|Hong Kong SAR
HM|Heard Island and McDonald Islands
HN|Honduras
HR|Croatia
HT|Haiti
HU|Hungary
ID|Indonesia
IE|Ireland
IL|Israel
IM|Isle of Man
IN|India
IO|British Indian Ocean Territory
IQ|Iraq
IR|Iran, Islamic Republic of|Iran
IS|Iceland
IT|Italy|Italia
JE|Jersey
JM|Jamaica
JO|Jordan
JP|Japan
KE|Kenya
KG|Kyrgyzstan
KH|Cambodia
KI|Kiribati
KM|Comoros
KN|Saint Kitts and Nevis
KP|Korea, Democratic People's Republic of|North Korea
KR|Korea, Republic of|South Korea|Republic of Korea
KW|Kuwait
KY|Cayman Islands
KZ|Kazakhstan
LA|Lao People's Democratic Republic|Laos
LB|Lebanon
LC|Saint Lucia
LI|Liechtenstein
LK|Sri Lanka
LR|Liberia
LS|Lesotho
LT|Lithuania
LU|Luxembourg
LV|Latvia
LY|Libya
MA|Morocco
MC|Monaco
MD|Moldova, Republic of|Moldova
ME|Montenegro
MF|Saint Martin (French part)
MG|Madagascar
MH|Marshall Islands
MK|North Macedonia
ML|Mali
MM|Myanmar|Burma
MN|Mongolia
MO|Macao|Macau
MP|Northern Mariana Islands
MQ|Martinique
MR|Mauritania
MS|Montserrat
MT|Malta
MU|Mauritius
MV|Maldives
MW|Malawi
MX|Mexico
MY|Malaysia
MZ|Mozambique
NA|Namibia
NC|New Caledonia
NE|Niger
NF|Norfolk Island
NG|Nigeria
NI|Nicaragua
NL|Netherlands, Kingdom of the|Netherlands|Holland
NO|Norway
NP|Nepal
NR|Nauru
NU|Niue
NZ|New Zealand
OM|Oman
PA|Panama
PE|Peru
PF|French Polynesia
PG|Papua New Guinea
PH|Philippines
PK|Pakistan
PL|Poland
PM|Saint Pierre and Miquelon
PN|Pitcairn
PR|Puerto Rico
PS|Palestine, State of|Palestine
PT|Portugal
PW|Palau
PY|Paraguay
QA|Qatar
RE|Réunion
RO|Romania
RS|Serbia
RU|Russian Federation|Russia
RW|Rwanda
SA|Saudi Arabia
SB|Solomon Islands
SC|Seychelles
SD|Sudan
SE|Sweden
SG|Singapore
SH|Saint Helena, Ascension and Tristan da Cunha|Saint Helena
SI|Slovenia
SJ|Svalbard and Jan Mayen
SK|Slovakia
SL|Sierra Leone
SM|San Marino
SN|Senegal
SO|Somalia
SR|Suriname
SS|South Sudan
ST|Sao Tome and Principe
SV|El Salvador
SX|Sint Maarten (Dutch part)
SY|Syrian Arab Republic|Syria
SZ|Eswatini|Swaziland
TC|Turks and Caicos Islands
TD|Chad
TF|French Southern Territories
TG|Togo
TH|Thailand
TJ|Tajikistan
TK|Tokelau
TL|Timor-Leste|East Timor
TM|Turkmenistan
TN|Tunisia
TO|Tonga
TR|Türkiye|Turkey
TT|Trinidad and Tobago
TV|Tuvalu
TW|Taiwan, Province of China|Taiwan
TZ|Tanzania, United Republic of|Tanzania
UA|Ukraine
UG|Uganda
UM|United States Minor Outlying Islands
US|United States of America|United States|USA|U.S.A.
UY|Uruguay
UZ|Uzbekistan
VA|Holy See|Vatican City
VC|Saint Vincent and the Grenadines
VE|Venezuela, Bolivarian Republic of|Venezuela
VG|Virgin Islands (British)|British Virgin Islands
VI|Virgin Islands (U.S.)|US Virgin Islands
VN|Viet Nam|Vietnam
VU|Vanuatu
WF|Wallis and Futuna
WS|Samoa
YE|Yemen
YT|Mayotte
ZA|South Africa
ZM|Zambia
ZW|Zimbabwe
"""


def _key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(c for c in decomposed if not unicodedata.combining(c))
    letters = letters.replace("'", "").replace("\u2019", "")  # apostrophes join: d'Ivoire
    words = re.sub(r"[^\w]+", " ", letters).split()
    if words[:2] == ["made", "in"]:
        words = words[2:]
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(words)


def _rows() -> list[list[str]]:
    return [line.split("|") for line in _TABLE.strip().splitlines()]


ISO_3166_ALPHA2 = frozenset(row[0] for row in _rows())
NAMES: dict[str, str] = {row[0]: row[1] for row in _rows()}  # code -> ISO short name
_BY_NAME: dict[str, str] = {_key(name): row[0] for row in _rows() for name in row[1:]}


def normalise_country(text: str) -> str:
    """ISO 3166-1 alpha-2 code for a printed country code or name."""
    stripped = text.strip()
    if stripped.upper() in ISO_3166_ALPHA2 and len(stripped) == 2:
        return stripped.upper()
    code = _BY_NAME.get(_key(stripped)) if stripped else None
    if code is None:
        raise NormalisationError(f"{text!r} is not a country name or ISO 3166-1 alpha-2 code")
    return code

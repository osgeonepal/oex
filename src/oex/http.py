"""One requests session that retries dropped connections and transient server errors."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

session = requests.Session()
_adapter = HTTPAdapter(
    max_retries=Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        retry_after_max=120,
    )
)
session.mount("https://", _adapter)
session.mount("http://", _adapter)

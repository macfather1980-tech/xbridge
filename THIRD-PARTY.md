# Third-party dependencies

`xbridge.py` uses two Python libraries. They are **not vendored** in this
repository: `install.sh` installs them from PyPI into a virtualenv, and each
installed copy remains under its own license.

| Package | Pinned by `install.sh` | License |
|---|---|---|
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | `zeroconf==0.148.0` | [LGPL-2.1-or-later](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html) |
| [ifaddr](https://github.com/pydron/ifaddr) | `ifaddr==0.2.0` | [MIT](https://opensource.org/licenses/MIT) |

If you bundle copies of these packages with a distribution of XBridge
(for example, inside a disk image), keep their `LICENSE` / `COPYING` files
alongside the package code, as their licenses require.

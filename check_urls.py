# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.32",
#     "urlextract>=1.10",
#     "dnspython>=2.6",
#     "tldextract>=5.1",
#     "whoisit>=3.0",
# ]
# ///
"""
Staged validation of the website URLs listed in the Japanese medical facility
open data (Iryo Joho Net, MHLW): hospitals, clinics, dental clinics, maternity
homes and pharmacies. The results serve as a proxy for how well domain names
and hostnames -- the "addresses" of the Internet -- are understood.

  Stage 0  Listing  : share of rows with / without a URL, over ALL rows (summary.csv)

  Stage 1  URL syntax : extract with urlextract. Common typos (http// htpp:// trailing
                        dot, ...) are repaired and recorded
  Stage 2  Hostname   : RFC 1123 syntax (characters, label length, hyphen position, dots)
  Stage 3  TLD        : Public Suffix List (tldextract): does the TLD exist, and what is
                        the registered domain
  Stage 4  DNS        : resolve A/AAAA with dnspython. On NXDOMAIN, also query NS of the
                        registered domain to tell "domain does not exist" from "only the
                        hostname part is wrong". When the hostname is gone but the parent
                        domain survives, query its CNAME: a dangling CNAME to a hosting
                        provider whose target is itself NXDOMAIN is flagged as a subdomain-
                        takeover candidate. Also try adding/removing "www."
  Stage 5  HTTP       : reachability with requests (only for hosts that resolved)
  Stage 6  RDAP        : for registered domains that are absent from DNS, query gTLD RDAP
                        (whoisit) to tell "available for registration" from "still
                        registered". TLDs without an RDAP endpoint (e.g. .jp) are reported
                        as unsupported; we do NOT fall back to port-43 WHOIS
  Extra    Scheme     : for URLs without a scheme, try both http and https and record
                        which one is correct

Output (--outdir):
  all_results.csv        result of every stage for every row
  invalid_urls.csv       rows with a stage-1 problem (with repair / reason)
  hostname_problems.csv  rows with a stage 2-4 problem (syntax, TLD, DNS)
  unreachable_urls.csv   unreachable at stage 5 (DNS resolved; excludes 401/403/429)
  access_denied_urls.csv 401/403/429 at stage 5 (server responded, so the address is
                         correct; with a WAF/CDN guess)
  scheme_completion.csv  http/https verdict for URLs without a scheme
  rdap_domains.csv       RDAP registration status per DNS-absent registered domain
                         (NOT for publication: it identifies takeable domains)
  subdomain_takeover.csv hostnames with a dangling CNAME to a claimable resource
                         (NOT for publication: it identifies takeable subdomains)
  hosts.csv              DNS result per unique hostname
  summary.csv            per-file aggregates (counts and percentages); published, ASCII,
                         English column names, no BOM

Note: the input CSVs have Japanese column names, and the category labels written to
the row-level output CSVs (stage, dns_status, scheme_verdict, ...) are in Japanese.
plot_results.R translates them to English for the figures (see EN_MAP there).
summary.csv is written with English column names and facility types (SUMMARY_COLS_EN).

Usage (uv provisions the dependencies from the PEP 723 metadata above):
  uv run check_urls.py
  uv run check_urls.py --limit 300          # quick test (limit hosts checked by DNS/HTTP)
  uv run check_urls.py --workers 64 --timeout 10
"""
import argparse
import csv
import io
import ipaddress
import os
import re
import sys
import tempfile
import threading
import time
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import dns.exception
import dns.resolver
import requests
import tldextract
import urllib3
import whoisit
import whoisit.errors as rdap_err
from urlextract import URLExtract


def write_text(path, text, bom=True, retries=6):
    """Write `text` to `path`, retrying transient filesystem failures.

    The output folder is frequently a Google Drive virtual filesystem that intermittently
    raises OSError (e.g. [Errno 22] Invalid argument / EINVAL) on open or write. Because this
    script does all of its DNS and HTTP work in memory and writes only at the very end, one
    such hiccup at the final step would otherwise discard a multi-hour run. We retry with a
    short backoff, and if the destination keeps failing we save a copy to a local temp folder
    and report its path so the measurement is never lost.
    """
    data = ("﻿" + text) if bom else text   # BOM byte-identical to utf-8-sig; added once
    last = None
    for attempt in range(retries):
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(data)
            return path
        except OSError as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    fallback_dir = os.path.join(tempfile.gettempdir(), "check_urls_out")
    os.makedirs(fallback_dir, exist_ok=True)
    local = os.path.join(fallback_dir, os.path.basename(path))
    with open(local, "w", encoding="utf-8", newline="") as f:
        f.write(data)
    print(f"  WARNING: could not write {path} after {retries} tries ({last!r}); "
          f"saved to {local} instead -- copy it to the output folder manually.", file=sys.stderr)
    return local


# File name -> (facility type, URL column, name column). The ID column is "ID" in every file.
# Values are the Japanese labels / column names used in the open data.
FILES = {
    "01-1_hospital_facility_info_20260601.csv": ("病院", "案内用ホームページアドレス", "正式名称"),
    "02-1_clinic_facility_info_20260601.csv": ("診療所", "案内用ホームページアドレス", "正式名称"),
    "03-1_dental_facility_info_20260601.csv": ("歯科診療所", "案内用ホームページアドレス", "正式名称"),
    "04_maternity_home_20260601.csv": ("助産所", "案内用ホームページアドレス", "正式名称"),
    "05_pharmacy_20260601.csv": ("薬局", "薬局のホームページアドレス", "名称"),
}
ID_COL = "ID"

# summary.csv is published, so its column names and facility types are written in English.
# (The row-level CSVs keep the Japanese category labels; see the module docstring.)
KIND_EN = {"病院": "Hospitals", "診療所": "Clinics", "歯科診療所": "Dental clinics",
           "助産所": "Maternity homes", "薬局": "Pharmacies", "合計": "Total"}
SUMMARY_COLS_EN = {
    "file": "file",
    "種別": "facility_type",
    "総行数": "total_rows",
    "URL記載あり": "url_listed",
    "URL記載あり 率%(全行中)": "url_listed_pct_of_all_rows",
    "URL記載なし": "url_not_listed",
    "URL記載なし 率%(全行中)": "url_not_listed_pct_of_all_rows",
    "判定行数": "evaluated_rows",
    # Metrics below are counts; each one's percentage column becomes "<metric>_pct" (denominator: evaluated_rows)
    "URL構文 正規": "url_syntax_valid",
    "URL構文 修正で救済": "url_syntax_repaired",
    "URL構文 修正不能": "url_syntax_unrecoverable",
    "スキーム欠落": "scheme_missing",
    "スキーム誤記": "scheme_misspelled",
    "ホスト名構文 問題あり": "hostname_syntax_issue",
    "TLDが実在しない": "tld_nonexistent",
    "DNS 解決可": "dns_resolved",
    "DNS ドメインが存在しない": "dns_domain_not_exist",
    "DNS ドメインは有るがホスト名が無い": "dns_domain_exists_hostname_not",
    "サブドメインテイクオーバー候補": "subdomain_takeover_candidate",
    "DNS www付け外しで解決可": "dns_resolves_with_www_toggled",
    "HTTP 到達可": "http_reachable",
    "HTTP 自動アクセス拒否(応答あり)": "http_access_denied",
    "HTTP 応答あり(到達可+拒否)": "http_responded",
    "HTTP 到達不可(DNSは可)": "http_unreachable",
    "スキーム補完 httpが正": "scheme_completion_http_correct",
    "スキーム補完 httpsが正": "scheme_completion_https_correct",
    # RDAP status of registered domains that are absent from DNS (denominator: evaluated_rows)
    "RDAP 登録可能": "dns_absent_rdap_available",
    "RDAP 登録済み": "dns_absent_rdap_registered",
    "RDAP 削除償還保留": "dns_absent_rdap_pending_delete",
    "RDAP 非対応TLD": "dns_absent_rdap_unsupported_tld",
    "RDAP 照会不可": "dns_absent_rdap_error",
    "総合OK(修正不要)": "overall_ok_no_correction",
    "スキーム補完 httpsが正 率%(判定できた欠落URL中)": "scheme_completion_https_pct_of_determined",
}


def summary_to_english(s: dict) -> dict:
    """Translate one summary row (Japanese keys and facility type) to English."""
    out = {}
    for k, v in s.items():
        if k in SUMMARY_COLS_EN:
            out[SUMMARY_COLS_EN[k]] = v
        elif k.endswith(" 率%") and k[:-3] in SUMMARY_COLS_EN:
            out[SUMMARY_COLS_EN[k[:-3]] + "_pct"] = v
        else:
            raise KeyError(f"no English name for summary column: {k}")
    out["facility_type"] = KIND_EN.get(out["facility_type"], out["facility_type"])
    if out["file"] == "合計":
        out["file"] = "Total"
    return out

# Identify honestly as a research client; we never impersonate a browser.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; url-reachability-check/1.0)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# whoisit emits a QueryWarning when a secondary RDAP sub-query (the registrar's server,
# followed from the registry record) 404s, rate-limits, or has a bad certificate. These
# do not change the top-level verdict (the registry answer already decides registered vs.
# available), so we silence them to keep the run log readable.
warnings.filterwarnings("ignore", category=rdap_err.QueryWarning)
_local = threading.local()


# ================================================================ Stage 1: URL syntax
# Leading "something that looks like a scheme" + separator.
# e.g. http// https: htpp:// www.https:// http//: http.// http::// https:: http:s// url:
SCHEME_RE = re.compile(
    r"^(?P<s>(?:www\.)?[a-z]{1,8}(?::s)?)(?P<sep>(?=[:;.,/]*[:/])[:;.,/]+)", re.I)


def repair(text: str):
    """Mechanically repair common typos. Returns (repaired text, list of fixes, scheme state)."""
    fixes = []
    scheme_state = "あり"                        # present
    # Duplicated scheme such as http://www.https://example.jp -> keep the latter
    m2 = re.match(r"^[a-z.]{1,12}[:;./]+(?:www\.)?(?=h[a-z]{1,6}[:;./]+)", text, re.I)
    if m2 and SCHEME_RE.match(text[m2.end():]):
        fixes.append("スキーム二重記述")          # duplicated scheme
        text = text[m2.end():]
    m = SCHEME_RE.match(text)
    if m and (":" in m.group(0) or "//" in m.group("sep")):
        s, sep = m.group("s").lower(), m.group("sep")
        if s not in ("http", "https") or sep != "://":
            new_s = "https" if s.endswith("s") else "http"
            fixes.append(f"スキーム誤記({m.group(0)} → {new_s}://)")   # misspelled scheme
            text = new_s + "://" + text[m.end():]
            scheme_state = "誤記修正"            # misspelled, corrected
    elif "://" not in text:
        scheme_state = "欠落"                    # missing
    # http://www//example.jp, http://www:example.jp -> http://www.example.jp
    t2 = re.sub(r"^(https?://)www[:/]+", r"\1www.", text, flags=re.I)
    if t2 != text:
        fixes.append("www の後の区切り誤記")      # wrong separator after www
        text = t2
    host = urlsplit(text if "://" in text else "http://" + text).netloc
    new_host = host
    if "," in new_host:
        fixes.append("ホスト名: ドットの代わりにカンマ")   # comma instead of dot
        new_host = new_host.replace(",", ".")
    if ".." in new_host:
        fixes.append("ホスト名: ドット連続（..）")        # consecutive dots
        new_host = re.sub(r"\.{2,}", ".", new_host)
    if new_host.endswith("."):
        fixes.append("ホスト名末尾にドット")              # trailing dot
        new_host = new_host.rstrip(".")
    if new_host.startswith("."):
        fixes.append("ホスト名先頭にドット")              # leading dot
        new_host = new_host.lstrip(".")
    if new_host != host:
        text = text.replace(host, new_host, 1)
    return text, fixes, scheme_state


def validate(raw: str, extractor: URLExtract):
    """
    Returns a dict:
      check_url   : URL used for the reachability check ("" if nothing could be extracted)
      host        : hostname (guessed even when extraction fails; used for DNS)
      scheme_state: あり (present) / 欠落 (missing) / 誤記修正 (misspelled, corrected)
      problems    : reasons the entry is considered irregular
    """
    problems = []
    text = raw.strip()
    if text != raw:
        problems.append("前後に空白")                     # leading/trailing whitespace
    if any(c in text for c in " 　\t\r\n"):
        problems.append("URL中に空白/制御文字")           # whitespace inside URL
        text = re.sub(r"[\s　]+", "", text)

    text, fixes, scheme_state = repair(text)
    problems += fixes
    guess_host = (urlsplit(text if "://" in text else "http://" + text).hostname or "").lower()

    out = {"check_url": "", "host": guess_host, "scheme_state": scheme_state}
    found = extractor.find_urls(text, only_unique=True)
    if not found:
        out["problems"] = problems + ["URLを抽出できない（修正不能）"]   # unrecoverable
        return out
    if len(found) > 1:
        problems.append(f"複数URL({len(found)}件)")       # multiple URLs
    url = found[0]
    if url != text:
        problems.append("抽出結果と原文が不一致")         # extra text around URL

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        if "://" in url:
            out["problems"] = problems + [f"非HTTPスキーム({parts.scheme})"]   # non-HTTP scheme
            return out
        if scheme_state == "あり":
            scheme_state = "欠落"
        problems.append("スキーム欠落")                   # missing scheme
        url = "http://" + url.lstrip("/")      # provisional; the correct scheme is decided at stage 5
        parts = urlsplit(url)
    if not parts.hostname:
        out["problems"] = problems + ["ホスト名なし"]     # no hostname
        return out
    if guess_host and parts.hostname.lower() != guess_host:
        # Partial extraction: when a full-width character splits the host, urlextract may
        # return only a trailing part (e.g. "shiso.jp"). That would check a different host,
        # so the extraction is rejected.
        out["problems"] = problems + [f"部分抽出のため不採用({parts.hostname})"]
        return out
    if "//" in parts.path:
        problems.append("パスに // を含む")               # double slash in path
    out.update(check_url=url, host=parts.hostname.lower(), scheme_state=scheme_state, problems=problems)
    return out


# ================================================================ Stages 2-3: hostname syntax / TLD
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def host_syntax(host: str, tld: tldextract.TLDExtract):
    """Returns (list of issues, public suffix, registered domain, whether DNS may be queried)."""
    issues = []
    if not host:
        return ["ホスト名なし"], "", "", False                    # no hostname
    try:
        ipaddress.ip_address(host.strip("[]"))
        return ["IPアドレス直書き"], "", "", False                # IP address literal
    except ValueError:
        pass
    if not host.isascii():
        if "." not in host:
            return ["URLではない記述（日本語等の文字列）"], "", "", False   # not a URL (Japanese text etc.)
        if re.search(r"[｡-ﾟ　-ヿ！-～]", host):
            issues.append("全角/半角カナ・全角記号が混入")         # full-width / kana characters
    labels0 = host.split(".")
    if any(not lb for lb in labels0):
        return ["空ラベル（.. や先頭/末尾のドット）"], "", "", False   # empty label
    try:
        ascii_host = ".".join(lb.encode("idna").decode("ascii") for lb in labels0)
    except UnicodeError:
        return issues + ["IDN変換不可（使用不可文字）"], "", "", False  # IDN conversion failed
    if ascii_host != host and not issues:
        issues.append("国際化ドメイン名(IDN)")                    # internationalized domain name
    if len(ascii_host) > 253:
        issues.append("ホスト名が253文字超")                      # hostname > 253 chars
    labels = ascii_host.split(".")
    if len(labels) < 2:
        issues.append("ドットなし（TLDなし）")                    # no dot (no TLD)
    for lb in labels:
        if not lb:
            issues.append("空ラベル（.. 等）")                    # empty label
        elif len(lb) > 63:
            issues.append("ラベルが63文字超")                     # label > 63 chars
        elif "_" in lb:
            issues.append("アンダースコアを含む")                 # underscore
        elif lb.startswith("-") or lb.endswith("-"):
            issues.append("ラベル先頭/末尾がハイフン")            # label starts/ends with hyphen
        elif not LABEL_RE.match(lb):
            issues.append("使用不可文字を含む")                   # invalid characters
    ext = tld(ascii_host)
    suffix = ext.suffix
    # tldextract >= 5.2 renamed registered_domain to top_domain_under_public_suffix
    reg = (ext.top_domain_under_public_suffix if hasattr(ext, "top_domain_under_public_suffix")
           else ext.registered_domain)
    if len(labels) >= 2 and not suffix:
        issues.append(f"実在しないTLD(.{labels[-1]})")            # nonexistent TLD
    elif suffix and not reg:
        issues.append("公開サフィックスのみ（登録ドメインなし）")  # public suffix only
    # Querying DNS is pointless for empty labels or names without a dot
    queryable = len(labels) >= 2 and all(labels) and len(ascii_host) <= 253
    return sorted(set(issues), key=issues.index), suffix, reg or "", queryable


# ================================================================ Stage 4: DNS
def resolver() -> dns.resolver.Resolver:
    """One resolver per thread, using the system's resolver configuration."""
    if not hasattr(_local, "r"):
        r = dns.resolver.Resolver()
        r.lifetime = 6
        r.timeout = 3
        _local.r = r
    return _local.r


def q(name: str, rtype: str):
    """Returns (status, values, CNAME target). status = OK/NXDOMAIN/NOANSWER/SERVFAIL/TIMEOUT/ERROR"""
    try:
        ans = resolver().resolve(name, rtype, raise_on_no_answer=True)
        cname = str(ans.canonical_name).rstrip(".")
        return "OK", [a.to_text() for a in ans], (cname if cname != name else "")
    except dns.resolver.NXDOMAIN:
        return "NXDOMAIN", [], ""
    except dns.resolver.NoAnswer:
        return "NOANSWER", [], ""
    except dns.resolver.NoNameservers:
        return "SERVFAIL", [], ""
    except dns.exception.Timeout:
        return "TIMEOUT", [], ""
    except Exception as e:                      # noqa: BLE001
        return f"ERROR({type(e).__name__})", [], ""


def to_ascii(host: str) -> str:
    return ".".join(lb.encode("idna").decode("ascii") for lb in host.split("."))


# Hosting services where deleting a resource leaves the hostname's CNAME pointing at a
# name that no longer resolves (NXDOMAIN). A third party can then re-create the resource
# and serve content under the facility's hostname, inheriting the parent domain's trust
# (subdomain takeover). Suffix set adapted from the community "can-i-take-over-xyz"
# catalogue; used only to label a dangling CNAME. We observe; we never claim a resource.
TAKEOVER_SUFFIXES = [
    ("GitHub Pages", "github.io"),
    ("Heroku", "herokuapp.com"), ("Heroku", "herokudns.com"), ("Heroku", "herokussl.com"),
    ("Azure", "azurewebsites.net"), ("Azure", "cloudapp.net"), ("Azure", "cloudapp.azure.com"),
    ("Azure", "trafficmanager.net"), ("Azure", "azureedge.net"), ("Azure", "blob.core.windows.net"),
    ("AWS", "amazonaws.com"), ("AWS", "elasticbeanstalk.com"), ("AWS CloudFront", "cloudfront.net"),
    ("Netlify", "netlify.app"), ("Netlify", "netlify.com"), ("Cloudflare Pages", "pages.dev"),
    ("Firebase", "web.app"), ("Firebase", "firebaseapp.com"), ("Shopify", "myshopify.com"),
    ("Surge", "surge.sh"), ("Bitbucket", "bitbucket.io"), ("Ghost", "ghost.io"),
    ("WP Engine", "wpengine.com"), ("Read the Docs", "readthedocs.io"), ("Fastly", "fastly.net"),
    ("Sakura", "sakura.ne.jp"), ("Xserver", "xsrv.jp"), ("Lolipop", "lolipop.jp"),
]


def takeover_provider(host: str) -> str:
    """Return the hosting provider if the host is under a takeover-prone suffix, else ''."""
    h = host.lower().rstrip(".")
    for name, suf in TAKEOVER_SUFFIXES:
        if h == suf or h.endswith("." + suf):
            return name
    return ""


def cname_target(name: str) -> str:
    """Return the CNAME target of a name (lowercased, no trailing dot), or '' if none."""
    try:
        ans = resolver().resolve(name, "CNAME")
        return str(ans[0].target).rstrip(".").lower()
    except Exception:                           # noqa: BLE001 (NXDOMAIN, NoAnswer, timeout, ...)
        return ""


def dns_check(host: str, reg: str):
    ascii_host = to_ascii(host)
    st, a, cname = q(ascii_host, "A")
    aaaa = []
    if st in ("OK", "NOANSWER"):
        st6, aaaa, cname6 = q(ascii_host, "AAAA")
        cname = cname or cname6
        if st == "NOANSWER" and st6 == "OK":
            st = "OK"
    res = {"dns_status": "", "dns_detail": st, "a": " ".join(a), "aaaa": " ".join(aaaa),
           "cname": cname, "domain_status": "", "www_alt": "", "dns_note": "", "takeover": ""}

    if st == "OK":
        res["dns_status"] = "解決可"                              # resolved
        addrs = [ipaddress.ip_address(x) for x in a + aaaa]
        if addrs and all(not x.is_global for x in addrs):
            res["dns_note"] = "非グローバルIPのみ（プライベート/ループバック等）"   # non-global IPs only
        return res

    # --- Not resolved: narrow down the cause
    if st == "NOANSWER":
        res["dns_status"] = "名前は存在するがアドレスなし"        # name exists, no address record
    elif st == "NXDOMAIN":
        res["dns_status"] = "ホスト名が存在しない"                # hostname does not exist
    else:
        res["dns_status"] = f"DNS応答異常({st})"                  # DNS error (SERVFAIL/TIMEOUT/...)

    if reg:
        reg_ascii = to_ascii(reg)
        if reg_ascii != ascii_host or st != "NXDOMAIN":
            ns_st, _, _ = q(reg_ascii, "NS")
            if ns_st == "NXDOMAIN":
                # registered domain does not exist (unregistered / expired / misspelled)
                res["domain_status"] = "登録ドメインが存在しない（未登録/失効/綴り誤り）"
            elif ns_st == "OK":
                # registered domain exists; the hostname part does not (typo / hosting service ended)
                res["domain_status"] = "登録ドメインは存在（ホスト名部分が無い: 誤り/サービス終了）"
            else:
                res["domain_status"] = f"登録ドメイン照会異常({ns_st})"   # error querying registered domain
        else:
            res["domain_status"] = "登録ドメインが存在しない（未登録/失効/綴り誤り）"
        if st == "NXDOMAIN" and res["domain_status"].startswith("登録ドメインが存在しない"):
            res["dns_status"] = "ドメインが存在しない"            # domain does not exist

    # Subdomain-takeover check (high-confidence only): the hostname no longer resolves, but
    # if it still carries a CNAME to a hosting provider whose target is itself NXDOMAIN, the
    # resource can be re-claimed under this hostname. Only for the "parent exists, hostname
    # gone" case; whole-domain loss is domain takeover, handled by RDAP. We only observe.
    if st == "NXDOMAIN" and res["dns_status"] != "ドメインが存在しない":
        tgt = cname_target(ascii_host)
        if tgt:
            res["cname"] = res["cname"] or tgt
            prov = takeover_provider(tgt)
            if prov and q(tgt, "A")[0] == "NXDOMAIN":
                res["takeover"] = f"サブドメインテイクオーバー候補({prov})"   # dangling CNAME to a claimable resource

    alt = ascii_host[4:] if ascii_host.startswith("www.") else "www." + ascii_host
    if q(alt, "A")[0] == "OK":
        res["www_alt"] = f"{alt} なら解決可"                      # "<alt> resolves"
    return res


# ================================================================ Stage 6: RDAP
# For a registered domain that is absent from DNS, RDAP tells "available for
# registration" (the domain can be taken over) from "still registered". Only gTLDs
# publish an RDAP endpoint in the IANA bootstrap; .jp and other ccTLDs that do not are
# reported as unsupported and left unqueried, because their port-43 WHOIS restricts use
# to network administration and we do not use it.
_rdap_lock = threading.Lock()


def rdap_bootstrap() -> None:
    with _rdap_lock:
        if not whoisit.is_bootstrapped():
            whoisit.bootstrap()


def rdap_check(domain: str):
    """Returns (status label, detail) for one registered domain absent from DNS."""
    if not domain:
        return "RDAP照会不可", "no registered domain"
    for attempt in range(3):
        try:
            r = whoisit.domain(domain, allow_insecure_ssl=True)
            st = [str(s).lower() for s in (r.get("status") or [])]
            if any("redemption" in s or "pending delete" in s for s in st):
                return "登録済み（削除/償還保留）", f"status={st}"   # registered, pending delete/redemption
            exp = r.get("expiration_date")
            return "登録済み", (f"expires={exp.date()}" if exp else f"status={st}")   # still registered
        except rdap_err.ResourceDoesNotExist:
            return "登録可能（RDAPで未登録）", ""                 # available for registration (404)
        except rdap_err.UnsupportedError:
            return "RDAP非対応TLD", "no gTLD RDAP endpoint (e.g. .jp); WHOIS not used"
        except rdap_err.RateLimitedError:
            time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:                                   # noqa: BLE001
            return "RDAP照会不可", f"{type(e).__name__}: {str(e)[:120]}"
    return "RDAP照会不可", "rate limited after retries"


# ================================================================ Stage 5: HTTP
def session() -> requests.Session:
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _local.s = s
    return _local.s


_ip_sem: dict = {}
_ip_sem_lock = threading.Lock()
PER_IP = 2                                       # concurrent connections per server IP (overridden in main)

# hostname -> key identifying the server, set from the DNS stage before the HTTP stage.
# The key is the sorted tuple of resolved A/AAAA addresses, so every hostname that
# resolves to the same server shares one semaphore (e.g. many co-hosted sites on one IP).
# Falls back to the hostname itself when no address is known.
_server_key_of_host: dict = {}


def set_server_keys(dns_res: dict) -> None:
    for h, d in dns_res.items():
        addrs = (d.get("a", "") + " " + d.get("aaaa", "")).split()
        _server_key_of_host[h] = tuple(sorted(addrs)) if addrs else ("host:" + h,)


def ip_slot(url: str) -> threading.Semaphore:
    h = urlsplit(url).hostname or ""
    key = _server_key_of_host.get(h) or ("host:" + h,)
    with _ip_sem_lock:
        if key not in _ip_sem:
            _ip_sem[key] = threading.Semaphore(PER_IP)
        return _ip_sem[key]


def http_check(url: str, timeout: float):
    """Check reachability while keeping concurrent requests to the same server IP at PER_IP."""
    with ip_slot(url):
        return _http_check(url, timeout)


def _http_check(url: str, timeout: float):
    """Returns dict(reachable, status, final_url, error, cert_ok, denied, waf)."""
    to = (min(timeout, 5), timeout)             # (connect, read)
    for attempt in ("HEAD", "GET", "GET_NOVERIFY"):
        try:
            if attempt == "HEAD":
                r = session().head(url, allow_redirects=True, timeout=to)
                if r.status_code >= 400:        # some servers mishandle HEAD; confirm with GET
                    continue
            else:
                r = session().get(url, allow_redirects=True, timeout=to, stream=True,
                                  verify=(attempt == "GET"))
                r.close()
            ok = r.status_code < 400
            return {"reachable": ok, "status": r.status_code, "final_url": r.url,
                    "error": "" if ok else f"HTTP {r.status_code}",
                    "cert_ok": attempt != "GET_NOVERIFY",
                    "denied": r.status_code in DENIED_STATUS,
                    "waf": detect_waf(r.headers) if r.status_code in DENIED_STATUS else ""}
        except requests.exceptions.SSLError:
            if attempt == "GET_NOVERIFY":
                return _err("SSLError", "failed even without certificate verification")
            continue                            # check whether only the certificate is the problem
        except requests.exceptions.RequestException as e:
            if attempt == "HEAD":
                continue
            return _err(type(e).__name__, e)
    return _err("Unknown", "")


def _err(kind, e):
    msg = str(e).replace("\n", " ")[:300]
    return {"reachable": False, "status": "", "final_url": "", "error": f"{kind}: {msg}", "cert_ok": "",
            "denied": False, "waf": ""}


# 401/403/429 mean "the server is alive and responded, but refused this request".
# The URL and hostname are therefore correct, so these are kept apart from
# "unreachable". We do NOT try to bypass them (e.g. by spoofing a browser User-Agent).
DENIED_STATUS = {401, 403, 429}
WAF_SIGNS = [   # (label, header name, substring of the value, or None = header present)
    ("Cloudflare", "cf-mitigated", None), ("Cloudflare", "cf-ray", None),
    ("Cloudflare", "server", "cloudflare"),
    ("Akamai", "server", "akamai"), ("Akamai", "akamai-grn", None),
    ("Amazon CloudFront/WAF", "x-amz-cf-id", None), ("AWS WAF", "x-amzn-waf-action", None),
    ("Imperva/Incapsula", "x-iinfo", None), ("Imperva/Incapsula", "x-cdn", "incapsula"),
    ("Sucuri", "x-sucuri-id", None), ("Fastly", "x-served-by", "cache-"),
    ("SiteGuard", "server", "siteguard"),
]


def detect_waf(headers) -> str:
    """Guess the WAF/CDN from response headers ("不明" = unknown)."""
    hits = []
    for name, key, needle in WAF_SIGNS:
        v = headers.get(key)
        if v is not None and (needle is None or needle in v.lower()) and name not in hits:
            hits.append(name)
    if not hits and headers.get("server"):
        return f"不明(server: {headers['server'][:40]})"
    return " / ".join(hits) or "不明"


def responded(res) -> bool:
    """The server responded (reachable, or denied automated access)."""
    return bool(res and (res["reachable"] or res.get("denied")))


def describe(res):
    """Short Japanese description of one HTTP result: 未チェック (not checked) /
    拒否 (denied) / 不可 (unreachable) / 可 (reachable) / 証明書不正 (invalid certificate)."""
    if res is None:
        return "未チェック"
    if res.get("denied"):
        return f"拒否({res['status']}・{res['waf']})"
    if not res["reachable"]:
        return f"不可({res['error'][:60]})"
    s = f"可({res['status']})"
    if res["cert_ok"] is False:
        s += "・証明書不正"
    return s


def scheme_verdict(http_res, https_res):
    """Decide the correct scheme for a URL listed without one. Returns (scheme, verdict label)."""
    h_ok = responded(http_res)                  # a 403 etc. still proves the scheme works
    s_ok = responded(https_res)
    s_cert = s_ok and https_res["cert_ok"] is True
    to_https = h_ok and str(http_res["final_url"]).startswith("https://")
    if s_cert and to_https:
        return "https", "https が正（http は https へ転送）"        # HTTPS (HTTP redirects to HTTPS)
    if s_cert and h_ok:
        return "https", "https が正（http でも到達可・転送なし）"  # HTTPS (HTTP also works, no redirect)
    if s_cert:
        return "https", "https が正（http では到達不可）"          # HTTPS (HTTP unreachable)
    if h_ok and s_ok:
        return "http", "http が正（https は証明書不正）"           # HTTP (HTTPS certificate invalid)
    if h_ok:
        return "http", "http が正（https では到達不可）"           # HTTP (HTTPS unreachable)
    if s_ok:
        return "https", "https のみ到達可（証明書不正）"           # HTTPS only (certificate invalid)
    return "", "どちらも到達不可"                                  # neither reachable


# ================================================================ main
def run_parallel(func, items, workers, label):
    out, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(func, it): it for it in items}
        for i, fut in enumerate(as_completed(futs), 1):
            out[futs[fut]] = fut.result()
            if i % 1000 == 0 or i == len(items):
                print(f"  [{label}] {i}/{len(items)}  {time.time() - t0:.0f}s", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description="Staged validation of facility website URLs.")
    ap.add_argument("--dir", default=".", help="folder containing the input CSVs")
    ap.add_argument("--outdir", default=".", help="folder for the output CSVs")
    ap.add_argument("--workers", type=int, default=32, help="number of parallel workers")
    ap.add_argument("--timeout", type=float, default=15, help="HTTP read timeout in seconds")
    ap.add_argument("--per-ip", "--per-host", dest="per_ip", type=int, default=2,
                    help="concurrent connections per server IP (sorted set of resolved A/AAAA)")
    ap.add_argument("--rdap-workers", type=int, default=8,
                    help="parallel workers for the RDAP stage (kept low to respect registry rate limits)")
    ap.add_argument("--no-rdap", action="store_true", help="skip the RDAP registration-availability stage")
    ap.add_argument("--limit", type=int, default=0, help="max hosts checked by DNS/HTTP (0 = all)")
    args = ap.parse_args()
    global PER_IP
    PER_IP = args.per_ip

    extractor = URLExtract()
    tld = tldextract.TLDExtract()

    # ---- Stages 1-3 (local processing)
    records, totals = [], Counter()
    syntax_cache = {}
    for fn, (kind, url_col, name_col) in FILES.items():
        with open(f"{args.dir}/{fn}", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if url_col not in reader.fieldnames:
                sys.exit(f"{fn}: URL column '{url_col}' not found")
            for row in reader:
                totals[(fn, "rows")] += 1
                raw = row.get(url_col) or ""
                if not raw.strip():
                    totals[(fn, "empty")] += 1
                    continue
                v = validate(raw, extractor)
                h = v["host"]
                if h not in syntax_cache:
                    syntax_cache[h] = host_syntax(h, tld)
                issues, suffix, reg, queryable = syntax_cache[h]
                records.append({
                    "file": fn, "kind": kind, "id": row[ID_COL], "name": row[name_col], "raw": raw,
                    "check_url": v["check_url"], "scheme_state": v["scheme_state"],
                    "url_problems": " / ".join(v["problems"]),
                    "host": h, "host_problems": " / ".join(issues),
                    "suffix": suffix, "registered_domain": reg, "_queryable": queryable,
                })

    # ---- Stage 4: DNS (per unique hostname)
    hosts = list(dict.fromkeys(r["host"] for r in records if r["_queryable"]))
    if args.limit:
        hosts = hosts[:args.limit]
    reg_of = {r["host"]: r["registered_domain"] for r in records}
    print(f"rows with URL: {len(records)}  unique hosts: {len(hosts)}", file=sys.stderr)
    dns_res = run_parallel(lambda h: dns_check(h, reg_of[h]), hosts, args.workers, "DNS")
    set_server_keys(dns_res)   # group hostnames by resolved server IP for the HTTP concurrency limit

    # ---- Stage 6: RDAP (registration status of registered domains absent from DNS)
    absent_domains = sorted({reg_of[h] for h, d in dns_res.items()
                             if d.get("dns_status") == "ドメインが存在しない" and reg_of.get(h)})
    rdap_res = {}
    if absent_domains and not args.no_rdap:
        rdap_bootstrap()
        print(f"DNS-absent registered domains to check via RDAP (gTLD only): {len(absent_domains)}",
              file=sys.stderr)
        rdap_res = run_parallel(rdap_check, absent_domains, args.rdap_workers, "RDAP")
    rdap_of = {dom: rdap_res.get(dom, ("", ""))[0] for dom in absent_domains}
    rdap_detail_of = {dom: rdap_res.get(dom, ("", ""))[1] for dom in absent_domains}

    # ---- Stage 5: HTTP (resolved hosts only; both http and https when the scheme is missing)
    urls = set()
    for r in records:
        d = dns_res.get(r["host"])
        if not r["check_url"] or not d or d["dns_status"] != "解決可":
            continue
        if r["scheme_state"] == "欠落":
            rest = r["check_url"].split("://", 1)[1]
            urls.update(("http://" + rest, "https://" + rest))
        else:
            urls.add(r["check_url"])
    urls = sorted(urls)
    print(f"URLs to check over HTTP: {len(urls)}", file=sys.stderr)
    http_res = run_parallel(lambda u: http_check(u, args.timeout), urls, args.workers, "HTTP")

    # ---- Merge per row
    rows = []
    for r in records:
        d = dns_res.get(r["host"])
        if r["_queryable"] and d is None:
            continue                            # skipped because of --limit
        row = {k: v for k, v in r.items() if not k.startswith("_")}
        # not queried (invalid hostname syntax) / not queried (no hostname)
        row.update(d or {"dns_status": "未照会（ホスト名構文不正）" if r["host"] else "未照会（ホスト名なし）"})
        row["rdap_status"] = rdap_of.get(r["registered_domain"], "")
        row["rdap_detail"] = rdap_detail_of.get(r["registered_domain"], "")
        h = None
        if r["check_url"] and d and d["dns_status"] == "解決可":
            if r["scheme_state"] == "欠落":
                rest = r["check_url"].split("://", 1)[1]
                hr, sr = http_res.get("http://" + rest), http_res.get("https://" + rest)
                scheme, verdict = scheme_verdict(hr, sr)
                row.update(scheme_http=describe(hr), scheme_https=describe(sr),
                           scheme_correct=scheme, scheme_verdict=verdict,
                           # http_completion_ok marks whether prefixing http:// was the right
                           # choice: "○" if http was correct, else the "https was correct" marker
                           http_completion_ok=("○" if scheme == "http" else "×（httpsが正）" if scheme == "https" else ""))
                h = sr if scheme == "https" else hr
            else:
                h = http_res.get(r["check_url"])
        elif r["scheme_state"] == "欠落":
            # undetermined (unrecoverable URL) / undetermined (DNS failure)
            row["scheme_verdict"] = "未判定（URL修正不能）" if not r["check_url"] else "未判定（DNS解決不可）"
        if h:
            row.update(reachable=h["reachable"], status=h["status"], final_url=h["final_url"],
                       http_error=h["error"], cert_ok=h["cert_ok"],
                       denied=h.get("denied", False), waf=h.get("waf", ""))
        # Overall verdict: the stage at which validation stopped
        if not r["check_url"] and not r["_queryable"]:
            stage = "1-2 URL/ホスト名構文で不正"          # invalid URL/hostname syntax
        elif row["dns_status"] != "解決可":
            stage = "4 DNSで不正"                          # DNS failure
        elif not r["check_url"]:
            stage = "1 URL構文で不正（DNSは解決可）"       # invalid URL syntax (DNS resolvable)
        elif row.get("reachable") is True:
            # OK after correction / OK
            stage = "OK（要修正あり）" if (r["url_problems"] or r["host_problems"]) else "OK"
        elif row.get("denied"):
            stage = "5' 到達可（自動アクセス拒否）"        # server responded (access denied)
        else:
            stage = "5 HTTPで到達不可"                     # HTTP unreachable
        row["stage"] = stage
        rows.append(row)

    # ---- Write (UTF-8 with BOM so that Excel opens the files correctly)
    fields = ["file", "kind", "id", "name", "raw", "check_url", "stage",
              "url_problems", "scheme_state",
              "host", "host_problems", "suffix", "registered_domain",
              "dns_status", "domain_status", "takeover", "www_alt", "dns_detail", "a", "aaaa", "cname", "dns_note",
              "rdap_status", "rdap_detail",
              "scheme_http", "scheme_https", "scheme_correct", "scheme_verdict", "http_completion_ok",
              "reachable", "status", "final_url", "http_error", "cert_ok", "denied", "waf"]

    def w(name, fieldnames, data, bom=True):
        # The row-level CSVs keep Japanese category labels, so they use utf-8-sig (BOM) for
        # Excel. summary.csv is published, all-ASCII, and written without a BOM (bom=False).
        # Build the file in memory first, then hand it to write_text, which retries: the
        # output folder is often a Google Drive virtual filesystem that intermittently fails
        # an open/write with OSError(EINVAL), and a single late failure must not discard a
        # multi-hour run.
        buf = io.StringIO()
        wr = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(data)
        write_text(f"{args.outdir}/{name}", buf.getvalue(), bom)

    w("all_results.csv", fields, rows)
    w("invalid_urls.csv", fields, [r for r in rows if r["url_problems"]])
    w("hostname_problems.csv", fields,
      [r for r in rows if r["host_problems"] or r["dns_status"] != "解決可" or r.get("dns_note")])
    w("unreachable_urls.csv", fields,
      [r for r in rows if r.get("reachable") is False and not r.get("denied")])
    w("access_denied_urls.csv", fields, [r for r in rows if r.get("denied")])
    w("scheme_completion.csv", fields, [r for r in rows if r["scheme_state"] == "欠落"])
    # RDAP registration status per DNS-absent registered domain, with the number of listed
    # URLs pointing to it. NOT for publication: it identifies domains open to takeover.
    fac_of_domain = Counter(r["registered_domain"] for r in rows
                            if r.get("rdap_status") and r["registered_domain"])
    w("rdap_domains.csv", ["registered_domain", "rdap_status", "rdap_detail", "listed_urls"],
      [{"registered_domain": dom, "rdap_status": rdap_of.get(dom, ""),
        "rdap_detail": rdap_detail_of.get(dom, ""), "listed_urls": fac_of_domain.get(dom, 0)}
       for dom in absent_domains])
    w("hosts.csv", ["host", "registered_domain", "dns_status", "domain_status", "takeover", "www_alt",
                    "dns_detail", "a", "aaaa", "cname", "dns_note"],
      [{"host": h, "registered_domain": reg_of[h], **d} for h, d in dns_res.items()])
    # Subdomain-takeover candidates (dangling CNAME to a claimable resource). NOT for
    # publication: it identifies hostnames open to takeover.
    w("subdomain_takeover.csv", fields, [r for r in rows if r.get("takeover")])

    # ---- Aggregates (one row per file, plus a grand-total row).
    # Built with Japanese keys and written to summary.csv with English column names
    # (SUMMARY_COLS_EN); plot_results.R reads total_rows and url_not_listed.
    summary = []
    for fn in list(FILES) + ["合計"]:
        rs = [r for r in rows if fn == "合計" or r["file"] == fn]
        n_rows = sum(v for (f, k), v in totals.items() if k == "rows" and fn in (f, "合計"))
        n_empty = sum(v for (f, k), v in totals.items() if k == "empty" and fn in (f, "合計"))
        n_has = n_rows - n_empty
        n = len(rs) or 1
        c = Counter()
        for r in rs:
            c["URL構文 正規"] += not r["url_problems"]                               # URL syntax regular
            c["URL構文 修正で救済"] += bool(r["url_problems"]) and bool(r["check_url"])  # rescued by repair
            c["URL構文 修正不能"] += not r["check_url"]                              # unrecoverable
            c["スキーム欠落"] += r["scheme_state"] == "欠落"                         # missing scheme
            c["スキーム誤記"] += r["scheme_state"] == "誤記修正"                     # misspelled scheme
            c["ホスト名構文 問題あり"] += bool(r["host_problems"])                   # hostname syntax issue
            c["TLDが実在しない"] += "実在しないTLD" in r["host_problems"]            # nonexistent TLD
            c["DNS 解決可"] += r["dns_status"] == "解決可"                           # DNS resolved
            c["DNS ドメインが存在しない"] += r["dns_status"] == "ドメインが存在しない"   # domain does not exist
            c["DNS ドメインは有るがホスト名が無い"] += "ホスト名部分が無い" in (r.get("domain_status") or "")
            c["サブドメインテイクオーバー候補"] += bool(r.get("takeover"))            # dangling CNAME to a claimable resource
            c["DNS www付け外しで解決可"] += bool(r.get("www_alt"))                   # resolves with www toggled
            c["HTTP 到達可"] += r.get("reachable") is True                          # reachable
            c["HTTP 自動アクセス拒否(応答あり)"] += bool(r.get("denied"))            # access denied
            c["HTTP 応答あり(到達可+拒否)"] += r.get("reachable") is True or bool(r.get("denied"))
            c["HTTP 到達不可(DNSは可)"] += r.get("reachable") is False and not r.get("denied")
            c["スキーム補完 httpが正"] += r.get("scheme_correct") == "http"
            c["スキーム補完 httpsが正"] += r.get("scheme_correct") == "https"
            # RDAP status of DNS-absent registered domains (counted per listed URL)
            c["RDAP 登録可能"] += r.get("rdap_status") == "登録可能（RDAPで未登録）"
            c["RDAP 登録済み"] += r.get("rdap_status") == "登録済み"
            c["RDAP 削除償還保留"] += r.get("rdap_status") == "登録済み（削除/償還保留）"
            c["RDAP 非対応TLD"] += r.get("rdap_status") == "RDAP非対応TLD"
            c["RDAP 照会不可"] += r.get("rdap_status") == "RDAP照会不可"
            c["総合OK(修正不要)"] += r["stage"] == "OK"                              # OK, no correction
        # Listed / not listed use ALL rows as the denominator; every other metric uses the
        # number of evaluated rows (listed URLs that were checked).
        s = {"file": fn, "種別": FILES[fn][0] if fn in FILES else "合計", "総行数": n_rows,
             "URL記載あり": n_has, "URL記載あり 率%(全行中)": round(n_has / (n_rows or 1) * 100, 2),
             "URL記載なし": n_empty, "URL記載なし 率%(全行中)": round(n_empty / (n_rows or 1) * 100, 2),
             "判定行数": len(rs)}
        for k, v in c.items():
            s[k] = v
            s[k + " 率%"] = round(v / n * 100, 2)
        n_sm = c["スキーム補完 httpが正"] + c["スキーム補完 httpsが正"]
        s["スキーム補完 httpsが正 率%(判定できた欠落URL中)"] = round(c["スキーム補完 httpsが正"] / n_sm * 100, 2) if n_sm else ""
        summary.append(s)
    summary_en = [summary_to_english(s) for s in summary]
    w("summary.csv", list(summary_en[-1].keys()), summary_en, bom=False)

    for s in summary:
        print(f"\n== {s['種別']}  {s['file']}", file=sys.stderr)
        print(f"  {'total rows':<22} {s['総行数']:>7}", file=sys.stderr)
        print(f"  {'URL listed':<22} {s['URL記載あり']:>7}  ({s['URL記載あり 率%(全行中)']}% of all rows)", file=sys.stderr)
        print(f"  {'no URL':<22} {s['URL記載なし']:>7}  ({s['URL記載なし 率%(全行中)']}% of all rows)", file=sys.stderr)
        print(f"  --- below: denominator = {s['判定行数']} evaluated rows", file=sys.stderr)
        for k, v in s.items():
            if "全行中" in k:
                continue
            if k.endswith("率%"):
                print(f"  {k[:-3]:<22} {s[k[:-3]]:>7}  ({v}%)", file=sys.stderr)

    # ---- RDAP summary (unique DNS-absent registered domains)
    if absent_domains:
        rc = Counter(rdap_of.get(dom, "") for dom in absent_domains)
        print(f"\n== RDAP: {len(absent_domains)} unique registered domains absent from DNS", file=sys.stderr)
        for label in ("登録可能（RDAPで未登録）", "登録済み", "登録済み（削除/償還保留）",
                      "RDAP非対応TLD", "RDAP照会不可"):
            print(f"  {label:<22} {rc.get(label, 0):>6}", file=sys.stderr)


if __name__ == "__main__":
    main()

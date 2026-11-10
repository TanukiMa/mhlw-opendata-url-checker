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
Supplement: subdomain-takeover candidates for a measurement run that predates the
dangling-CNAME check added to check_urls.py.

It reads <outdir>/hosts.csv, selects the hostnames that no longer resolve while their
parent (registered) domain survives, and runs only the high-confidence check: a dangling
CNAME to a hosting provider whose target is itself NXDOMAIN. It writes
<outdir>/subdomain_takeover.csv and prints a short summary. It makes no HTTP requests and
does not touch the other result files, so it does not require re-running the main
measurement. A full run of the current check_urls.py performs this inline (Stage 4).

Usage:
  uv run subdomain_takeover_supplement.py --outdir results
"""
import argparse
import csv
import sys

# Reuse the exact logic from the main script so the supplement and a full run agree.
import check_urls as cu


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", default="results", help="folder holding hosts.csv (output goes here too)")
    ap.add_argument("--workers", type=int, default=16, help="parallel DNS workers")
    args = ap.parse_args()

    with open(f"{args.outdir}/hosts.csv", encoding="utf-8-sig", newline="") as f:
        hosts = list(csv.DictReader(f))

    # "hostname gone, parent domain survives" — the only bucket where a dangling CNAME can
    # be taken over. Whole-domain loss is domain takeover (handled by RDAP), not this.
    targets = [h["host"] for h in hosts
               if h.get("dns_status") == "ホスト名が存在しない"
               and "ホスト名部分が無い" in (h.get("domain_status") or "")]
    print(f"hostnames gone while parent survives: {len(targets)}", file=sys.stderr)

    def check(host):
        tgt = cu.cname_target(cu.to_ascii(host))
        if not tgt:
            return ("", "", "")                      # no CNAME: not a dangling pointer
        prov = cu.takeover_provider(tgt)
        if prov and cu.q(tgt, "A")[0] == "NXDOMAIN":
            return (f"サブドメインテイクオーバー候補({prov})", tgt, prov)   # dangling CNAME to a claimable resource
        return ("", tgt, prov)                        # CNAME present but target resolves or not a known provider

    res = cu.run_parallel(check, targets, args.workers, "CNAME")
    rows = [{"host": h, "takeover": v[0], "cname": v[1], "provider": v[2]}
            for h, v in res.items() if v[0]]
    rows.sort(key=lambda r: r["provider"])

    out = f"{args.outdir}/subdomain_takeover.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["host", "provider", "cname", "takeover"])
        wr.writeheader()
        wr.writerows(rows)

    from collections import Counter
    by_prov = Counter(r["provider"] for r in rows)
    print(f"\nsubdomain-takeover candidates: {len(rows)}  (written to {out})", file=sys.stderr)
    for prov, n in by_prov.most_common():
        print(f"  {prov:<18} {n:>4}", file=sys.stderr)


if __name__ == "__main__":
    main()

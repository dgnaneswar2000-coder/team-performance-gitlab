"""Run the pipeline: pull merged MRs from GitLab, then rebuild staging and marts.

    python run_pipeline.py                  # incremental: only missing or recent days
    python run_pipeline.py --full-refresh   # re-pull every day in the lookback window
    python run_pipeline.py --transform-only # rebuild tables from raw, no API calls
    python run_pipeline.py --every 360      # keep running, once every 6 hours

Environment: LOOKBACK_DAYS (default 30), GITLAB_TOKEN (optional), ANON_SALT.
"""
import argparse
import logging
import time

from pipeline import ingest, transform


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--full-refresh", action="store_true")
    p.add_argument("--transform-only", action="store_true")
    p.add_argument("--every", type=int, metavar="MINUTES", help="repeat on a fixed interval")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    while True:
        conn = ingest.connect()
        if not args.transform_only:
            ingest.run(full_refresh=args.full_refresh, conn=conn)
        transform.run(conn)
        conn.close()
        if not args.every:
            break
        logging.info("sleeping %d minutes until next run", args.every)
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()

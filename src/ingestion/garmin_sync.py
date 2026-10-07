#!/usr/bin/env python3
"""
Garmin Connect Synchronization
Stahuje tréningová data z Garmin Connect a ukládá je lokálně.
Podporuje 2FA přes garth token persistence.

Použití:
    python garmin_sync.py --test                    # test spojení
    python garmin_sync.py --last-week               # poslední týden aktivit
    python garmin_sync.py --date 2024-01-01         # konkrétní datum
    python garmin_sync.py --range 2024-01-01 2024-01-31  # rozsah dat
    python garmin_sync.py --daily --last-month      # denní data za měsíc
"""

import os
import sys
import argparse
import json
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, List, Dict, Any

# Fix Windows console encoding pro ceske znaky
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

try:
    from garminconnect import Garmin
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Chybi dependencies: {e}")
    print("Aktivuj venv a nainstaluj: pip install garminconnect python-dotenv")
    exit(1)


class GarminSync:
    """Spravce synchronizace dat z Garmin Connect s 2FA supportem."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.data_dir = project_root / "data"
        self.activities_dir = self.data_dir / "activities"
        self.daily_dir = self.data_dir / "daily"
        self.tokens_dir = self.data_dir / "garth_tokens"

        # Vytvor adresare
        for d in [self.activities_dir, self.daily_dir, self.tokens_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Nacti .env
        load_dotenv(project_root / ".env")
        self.garmin_email = os.getenv("GARMIN_EMAIL")
        self.garmin_password = os.getenv("GARMIN_PASSWORD")

        if not self.garmin_email or not self.garmin_password:
            raise ValueError(
                "Garmin credentials nejsou nastaveny! Vyplň .env soubor."
            )

        self.garmin: Optional[Garmin] = None

    def connect(self, max_retries: int = 3) -> bool:
        """Pripoji se k Garmin Connect (garminconnect nativni login).

        Pouzije ulozene tokeny z tokens_dir; pokud chybi/expirovaly,
        udela novy login (muze vyzadovat MFA kod) a tokeny ulozi.
        """
        print(f"Prihlasuji se k Garmin Connect ({self.garmin_email})...")

        for attempt in range(1, max_retries + 1):
            try:
                self.garmin = Garmin(
                    self.garmin_email, self.garmin_password,
                    prompt_mfa=self._prompt_mfa,
                )
                self.garmin.login(tokenstore=str(self.tokens_dir))

                print(f"Uspesne prihlasen jako: {self.garmin.get_full_name()}")
                print(f"Tokeny ulozeny do {self.tokens_dir} (priste bez 2FA)")
                return True

            except Exception as e:
                if "429" in str(e) and attempt < max_retries:
                    wait = 30 * attempt
                    print(f"Rate limit (429), cekam {wait}s... (pokus {attempt}/{max_retries})")
                    time.sleep(wait)
                else:
                    print(f"Chyba pri prihlaseni: {e}")
                    return False

        return False

    @staticmethod
    def _prompt_mfa() -> str:
        """Vyzve uzivatele k zadani MFA kodu."""
        print("\n--- 2FA kod ---")
        print("Zadej MFA kod z SMS/authenticatoru:")
        code = input("> ").strip()
        return code

    def get_activities_for_date_range(
        self,
        start_date: date,
        end_date: date
    ) -> List[Dict[Any, Any]]:
        """Stahne seznam aktivit pro zadany rozsah."""
        if not self.garmin:
            raise RuntimeError("Nejdriv se pripoj pomoci connect()")

        print(f"Stahuji aktivity: {start_date} az {end_date}")
        try:
            activities = self.garmin.get_activities_by_date(
                start_date.isoformat(),
                end_date.isoformat()
            )
            print(f"Nalezeno {len(activities)} aktivit")
            return activities
        except Exception as e:
            print(f"Chyba pri stahovani aktivit: {e}")
            return []

    def save_activity_details(self, activity: Dict[Any, Any]) -> None:
        """Ulozi detaily aktivity vcetne GPX/FIT dat."""
        activity_id = activity.get('activityId')
        if not activity_id:
            return

        activity_file = self.activities_dir / f"activity_{activity_id}.json"

        # Preskoc pokud uz mame
        if activity_file.exists():
            return

        try:
            # Detaily aktivity
            activity_details = self.garmin.get_activity(activity_id)

            # FIT data (originalni format se vsim)
            try:
                fit_data = self.garmin.download_activity(
                    activity_id,
                    dl_fmt=self.garmin.ActivityDownloadFormat.ORIGINAL
                )
                if fit_data:
                    fit_file = self.activities_dir / f"activity_{activity_id}.zip"
                    with open(fit_file, 'wb') as f:
                        f.write(fit_data)
            except Exception as e:
                print(f"  FIT data nelze stahnout: {e}")

            # GPX data
            try:
                gpx_data = self.garmin.download_activity(
                    activity_id,
                    dl_fmt=self.garmin.ActivityDownloadFormat.GPX
                )
                if gpx_data:
                    gpx_file = self.activities_dir / f"activity_{activity_id}.gpx"
                    with open(gpx_file, 'wb') as f:
                        f.write(gpx_data)
            except Exception as e:
                print(f"  GPX data nelze stahnout: {e}")

            # Uloz JSON
            with open(activity_file, 'w', encoding='utf-8') as f:
                json.dump(activity_details, f, indent=2, ensure_ascii=False, default=str)

            print(f"  Ulozeno: {activity_file.name}")

        except Exception as e:
            print(f"  Chyba pri ukladani aktivity {activity_id}: {e}")

    def get_daily_summary(self, target_date: date) -> Optional[Dict[Any, Any]]:
        """Stahne denni souhrn (spanek, stress, kroky, atd.)."""
        try:
            daily_stats: Dict[str, Any] = {'date': target_date.isoformat()}

            daily_stats['summary'] = self.garmin.get_user_summary(target_date.isoformat())

            for key, method in [
                ('stress', 'get_all_day_stress'),
                ('body_battery', 'get_body_battery'),
                ('hrv', 'get_hrv_data'),
                ('sleep', 'get_sleep_data'),
                ('steps', 'get_daily_steps'),
                ('heart_rates', 'get_heart_rates'),
                ('respiration', 'get_respiration_data'),
                ('spo2', 'get_spo2_data'),
                ('rhr', 'get_rhr_day'),
                ('training_readiness', 'get_training_readiness'),
                ('training_status', 'get_training_status'),
                ('endurance_score', 'get_endurance_score'),
                ('body_composition', 'get_body_composition'),
                ('hydration', 'get_hydration_data'),
                ('max_metrics', 'get_max_metrics'),
                ('intensity_minutes', 'get_intensity_minutes_data'),
                ('floors', 'get_floors'),
            ]:
                try:
                    daily_stats[key] = getattr(self.garmin, method)(target_date.isoformat())
                except Exception:
                    pass

            return daily_stats

        except Exception as e:
            print(f"Chyba pri stahovani dennich dat pro {target_date}: {e}")
            return None

    def save_daily_summary(self, target_date: date, data: Dict[Any, Any]) -> None:
        """Ulozi denni souhrn."""
        daily_file = self.daily_dir / f"daily_{target_date.isoformat()}.json"
        with open(daily_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        print(f"  Ulozeno: {daily_file.name}")

    def sync_activities(self, start_date: date, end_date: date) -> None:
        """Synchronizuje aktivity pro zadane obdobi."""
        activities = self.get_activities_for_date_range(start_date, end_date)

        for i, activity in enumerate(activities, 1):
            activity_name = activity.get('activityName', 'Neznama aktivita')
            try:
                print(f"[{i}/{len(activities)}] {activity_name}")
            except UnicodeEncodeError:
                print(f"[{i}/{len(activities)}] {activity_name.encode('ascii', 'replace').decode()}")
            self.save_activity_details(activity)

    def sync_daily_data(self, start_date: date, end_date: date) -> None:
        """Synchronizuje denni data pro zadane obdobi."""
        print(f"Synchronizuji denni data: {start_date} az {end_date}")

        current_date = start_date
        while current_date <= end_date:
            daily_file = self.daily_dir / f"daily_{current_date.isoformat()}.json"

            if not daily_file.exists():
                print(f"Stahuji: {current_date}")
                daily_data = self.get_daily_summary(current_date)
                if daily_data:
                    self.save_daily_summary(current_date, daily_data)
            else:
                print(f"Preskakuji: {current_date} (uz existuje)")

            current_date += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description='Garmin Connect synchronization')

    parser.add_argument('--test', action='store_true', help='Test pripojeni')
    parser.add_argument('--daily', action='store_true', help='Stahni denni data misto aktivit')

    parser.add_argument('--last-week', action='store_true', help='Posledni tyden')
    parser.add_argument('--last-month', action='store_true', help='Posledni mesic')
    parser.add_argument('--date', type=str, help='Konkretni datum (YYYY-MM-DD)')
    parser.add_argument('--range', nargs=2, type=str, help='Rozsah dat (YYYY-MM-DD YYYY-MM-DD)')

    args = parser.parse_args()

    # Project root
    current_file = Path(__file__).resolve()
    project_root = current_file.parent.parent.parent

    try:
        sync = GarminSync(project_root)
    except ValueError as e:
        print(e)
        return 1

    if not sync.connect():
        return 1

    if args.test:
        print("Test pripojeni uspesny!")
        return 0

    # Datum rozsah
    end_date = date.today()

    if args.last_week:
        start_date = end_date - timedelta(days=7)
    elif args.last_month:
        start_date = end_date - timedelta(days=30)
    elif args.date:
        start_date = end_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    elif args.range:
        start_date = datetime.strptime(args.range[0], '%Y-%m-%d').date()
        end_date = datetime.strptime(args.range[1], '%Y-%m-%d').date()
    else:
        start_date = end_date - timedelta(days=2)

    try:
        if args.daily:
            sync.sync_daily_data(start_date, end_date)
        else:
            sync.sync_activities(start_date, end_date)
        print("Synchronizace dokoncena!")
    except Exception as e:
        print(f"Chyba behem synchronizace: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())

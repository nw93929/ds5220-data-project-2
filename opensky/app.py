import io
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import requests
import seaborn as sns
from boto3.dynamodb.conditions import Key

matplotlib.use("Agg")

OPENSKY_URL = "https://opensky-network.org/api/states/all"
# Continental US bounding box
PARAMS = {"lamin": 24.7, "lomin": -125.0, "lamax": 49.4, "lomax": -66.9}

REGION_ID  = "CONUS"
TABLE_NAME = os.environ["DYNAMODB_TABLE"]
S3_BUCKET  = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def fetch_flights():
    resp = requests.get(OPENSKY_URL, params=PARAMS, timeout=30)
    resp.raise_for_status()
    states = resp.json().get("states") or []

    airborne   = [s for s in states if not s[8]]
    altitudes  = [s[7] for s in airborne if s[7] is not None]
    velocities = [s[9] for s in airborne if s[9] is not None]

    return {
        "region":         REGION_ID,
        "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aircraft_count": Decimal(str(len(airborne))),
        "on_ground":      Decimal(str(len(states) - len(airborne))),
        "avg_altitude_m": Decimal(str(round(sum(altitudes) / len(altitudes), 1))) if altitudes else Decimal("0"),
        "avg_velocity_ms": Decimal(str(round(sum(velocities) / len(velocities), 2))) if velocities else Decimal("0"),
    }


def get_history(table):
    items, kwargs = [], dict(
        KeyConditionExpression=Key("region").eq(REGION_ID),
        ScanIndexForward=True,
    )
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)
    df["timestamp"]      = pd.to_datetime(df["timestamp"])
    df["aircraft_count"] = df["aircraft_count"].astype(int)
    df["avg_altitude_m"] = df["avg_altitude_m"].astype(float)
    return df.sort_values("timestamp").reset_index(drop=True)


def generate_plot(df):
    if len(df) < 2:
        return None

    sns.set_theme(style="darkgrid", context="talk", font_scale=0.9)
    fig, ax = plt.subplots(figsize=(14, 6))

    sns.lineplot(data=df, x="timestamp", y="aircraft_count",
                 ax=ax, color="#4FC3F7", linewidth=2.5)
    ax.fill_between(df["timestamp"], df["aircraft_count"],
                    df["aircraft_count"].min() * 0.95,
                    alpha=0.12, color="#4FC3F7")

    ax.set_title(
        "Airborne Aircraft Over Continental US\n"
        f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        fontsize=14, fontweight="bold", pad=14,
    )
    ax.set_xlabel("Time (UTC)", labelpad=8)
    ax.set_ylabel("Airborne Aircraft", labelpad=8)

    sns.despine(ax=ax, top=True, right=True)
    fig.autofmt_xdate(rotation=25, ha="right")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


def push_to_s3(buf, df):
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=S3_BUCKET, Key="plot.png",
                  Body=buf.getvalue(), ContentType="image/png")

    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)
    s3.put_object(Bucket=S3_BUCKET, Key="data.csv",
                  Body=csv_buf.getvalue(), ContentType="text/csv")


def main():
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table    = dynamodb.Table(TABLE_NAME)

    entry = fetch_flights()
    table.put_item(Item=entry)

    print(f"CONUS | aircraft={entry['aircraft_count']} | "
          f"avg_alt={entry['avg_altitude_m']}m | avg_vel={entry['avg_velocity_ms']}m/s")

    df  = get_history(table)
    buf = generate_plot(df)
    if buf:
        push_to_s3(buf, df)


if __name__ == "__main__":
    main()

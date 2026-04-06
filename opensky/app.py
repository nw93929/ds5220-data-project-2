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

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
COINS = ["bitcoin", "ethereum", "solana"]

TABLE_NAME = os.environ["DYNAMODB_TABLE"]
S3_BUCKET  = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def fetch_prices():
    resp = requests.get(
        COINGECKO_URL,
        params={"vs_currency": "usd", "ids": ",".join(COINS)},
        timeout=15,
    )
    resp.raise_for_status()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = []
    for coin in resp.json():
        records.append({
            "coin_id":   coin["id"],
            "timestamp": ts,
            "price_usd": Decimal(str(round(coin["current_price"], 2))),
            "market_cap": Decimal(str(coin["market_cap"])),
            "volume_24h": Decimal(str(coin["total_volume"])),
            "change_24h": Decimal(str(round(coin["price_change_percentage_24h"], 4))),
        })
    return records


def get_history(table, coin_id):
    items, kwargs = [], dict(
        KeyConditionExpression=Key("coin_id").eq(coin_id),
        ScanIndexForward=True,
    )
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def generate_plot(table):
    all_items = []
    for coin in COINS:
        all_items.extend(get_history(table, coin))

    if not all_items:
        return None

    df = pd.DataFrame(all_items)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["price_usd"] = df["price_usd"].astype(float)
    df = df.sort_values("timestamp")

    if df.groupby("coin_id")["timestamp"].count().min() < 2:
        return None

    sns.set_theme(style="darkgrid", context="talk", font_scale=0.85)
    fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True)

    colors = {"bitcoin": "#F7931A", "ethereum": "#627EEA", "solana": "#9945FF"}
    labels = {"bitcoin": "Bitcoin (BTC)", "ethereum": "Ethereum (ETH)", "solana": "Solana (SOL)"}

    for ax, coin in zip(axes, COINS):
        cdf = df[df["coin_id"] == coin]
        ax.plot(cdf["timestamp"], cdf["price_usd"], color=colors[coin], linewidth=2.5)
        ax.fill_between(cdf["timestamp"], cdf["price_usd"],
                        cdf["price_usd"].min() * 0.995, alpha=0.12, color=colors[coin])
        ax.set_ylabel(f"{labels[coin]}\nPrice (USD)", labelpad=8)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        sns.despine(ax=ax, top=True, right=True)

    axes[0].set_title(
        "Crypto Prices Over Time\n"
        f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        fontsize=14, fontweight="bold", pad=14,
    )
    axes[-1].set_xlabel("Time (UTC)", labelpad=8)
    fig.autofmt_xdate(rotation=25, ha="right")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf, df


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

    records = fetch_prices()
    for record in records:
        table.put_item(Item=record)
        print(f"{record['coin_id']} | ${record['price_usd']} | 24h: {record['change_24h']}%")

    result = generate_plot(table)
    if result:
        buf, df = result
        push_to_s3(buf, df)


if __name__ == "__main__":
    main()

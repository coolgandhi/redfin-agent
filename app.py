#!/usr/bin/env python3
"""
Redfin Listings Dashboard
Run with: python3 -m streamlit run app.py
"""

import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listings.db")

st.set_page_config(
    page_title="Redfin Dashboard",
    page_icon="🏠",
    layout="wide",
)

# ─── Data loading ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_data():
    if not os.path.exists(DB_FILE):
        return pd.DataFrame()
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql("SELECT * FROM listings", conn)
    conn.close()

    def to_num(series):
        return pd.to_numeric(
            series.astype(str).str.replace(r"[$,\s]", "", regex=True),
            errors="coerce",
        )

    df["date"]          = pd.to_datetime(df["date"], errors="coerce")
    df["first_seen"]    = pd.to_datetime(df["first_seen"], errors="coerce")
    df["price"]         = to_num(df["price"])
    df["sqft"]          = to_num(df["sqft"])
    df["price_psf"]     = to_num(df["price_psf"])
    df["hoa_adj_price"] = to_num(df["hoa_adj_price"])
    df["price_drops"]   = to_num(df["price_drops"]).fillna(0).astype(int)
    df["total_drop"]    = to_num(df["total_drop"]).fillna(0)
    df["rating1"]       = to_num(df["rating1"])
    df["rating2"]       = to_num(df["rating2"])
    df["rating3"]       = to_num(df["rating3"])
    df["beds"]          = to_num(df["beds"])
    df["baths"]         = to_num(df["baths"])
    df["city"]          = df["city"].str.strip().str.title()
    df["status"]        = df["status"].str.strip()

    df["top_rating"] = df[["rating1", "rating2", "rating3"]].max(axis=1)

    # Per-type ratings: look through type1/type2/type3 for a keyword match
    ELEMENTARY_TYPES = {"elementary", "k-5", "k-8", "k-12", "prek"}
    MIDDLE_TYPES     = {"middle", "k-8", "k-12"}
    HIGH_TYPES       = {"high", "k-12"}

    def _typed_rating(row, type_set):
        for i in [1, 2, 3]:
            t = str(row[f"type{i}"] or "").strip().lower()
            r = row[f"rating{i}"]
            if t in type_set and pd.notna(r):
                return r
        return float("nan")

    df["elementary_rating"] = df.apply(lambda r: _typed_rating(r, ELEMENTARY_TYPES), axis=1)
    df["middle_rating"]     = df.apply(lambda r: _typed_rating(r, MIDDLE_TYPES),     axis=1)
    df["high_rating"]       = df.apply(lambda r: _typed_rating(r, HIGH_TYPES),       axis=1)

    return df


df = load_data()

st.title("🏠 Redfin Listings Dashboard")

if df.empty:
    st.warning("No data found. Run `python3 redfin_agent.py` first to populate the database.")
    st.stop()

# ─── Sidebar filters ──────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Filters")

    sel_cities    = st.multiselect("City", sorted(df["city"].dropna().unique()))
    sel_zips      = st.multiselect("Zip Code", sorted(df["zip"].dropna().unique()))
    sel_status    = st.multiselect("Status", sorted(df["status"].dropna().unique()))
    prop_types    = sorted(df["property_type"].replace("", None).dropna().unique())
    sel_prop_type = st.multiselect("Property Type", prop_types)

    # Beds / Baths — show as numeric options, skip NaN
    bed_opts  = sorted(df["beds"].dropna().unique())
    bath_opts = sorted(df["baths"].dropna().unique())
    sel_beds  = st.multiselect(
        "Beds",
        options=bed_opts,
        format_func=lambda x: f"{int(x)} bd",
    )
    sel_baths = st.multiselect(
        "Baths",
        options=bath_opts,
        format_func=lambda x: f"{x:g} ba",
    )

    # Price range
    valid_prices = df["price"].dropna()
    p_min = int(valid_prices.min()) if not valid_prices.empty else 0
    p_max = int(valid_prices.max()) if not valid_prices.empty else 5_000_000
    sel_price = st.slider("Price Range", p_min, p_max, (p_min, p_max),
                          step=25_000, format="$%d")

    # School rating
    st.subheader("School Rating")
    school_types = st.multiselect(
        "School type(s)",
        ["Elementary", "Middle", "High"],
        default=[],
        placeholder="Any (leave empty for all)",
    )
    sel_rating = st.slider("Min Rating", 0, 10, 0)

    if st.button("Reset Filters"):
        st.cache_data.clear()
        st.rerun()

# ─── Apply filters ────────────────────────────────────────────────────────────

filtered = df.copy()

if sel_cities:
    filtered = filtered[filtered["city"].isin(sel_cities)]
if sel_zips:
    filtered = filtered[filtered["zip"].isin(sel_zips)]
if sel_status:
    filtered = filtered[filtered["status"].isin(sel_status)]
if sel_prop_type:
    filtered = filtered[filtered["property_type"].isin(sel_prop_type)]
if sel_beds:
    filtered = filtered[filtered["beds"].isin(sel_beds)]
if sel_baths:
    filtered = filtered[filtered["baths"].isin(sel_baths)]

filtered = filtered[
    (filtered["price"] >= sel_price[0]) & (filtered["price"] <= sel_price[1])
]

if sel_rating > 0:
    type_col = {
        "Elementary": "elementary_rating",
        "Middle":     "middle_rating",
        "High":       "high_rating",
    }
    if not school_types:
        # No type selected — apply to the best rating across all schools
        filtered = filtered[filtered["top_rating"] >= sel_rating]
    else:
        # Keep listings where ALL selected school types meet the threshold
        mask = pd.Series(True, index=filtered.index)
        for t in school_types:
            mask &= (filtered[type_col[t]] >= sel_rating)
        filtered = filtered[mask]

# ─── Summary metrics (reflect active filters) ─────────────────────────────────

st.caption(f"Showing **{len(filtered):,}** of {len(df):,} listings")

_psf_base = filtered.dropna(subset=["price", "sqft"])
_wtd_psf  = (_psf_base["price"].sum() / _psf_base["sqft"].sum()) if not _psf_base.empty else None

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Listings",        f"{len(filtered):,}")
c2.metric("Avg Price",       f"${filtered['price'].mean():,.0f}"    if filtered['price'].notna().any()     else "—")
c3.metric("Avg $/SqFt",      f"${filtered['price_psf'].mean():,.0f}" if filtered['price_psf'].notna().any() else "—")
c4.metric("Wtd $/SqFt",      f"${_wtd_psf:,.0f}" if _wtd_psf else "—",
          help="Total price ÷ total sqft across filtered listings")
c5.metric("Price Drops",     f"{int(filtered['price_drops'].sum()):,}")

st.divider()

# ─── Tabs ─────────────────────────────────────────────────────────────────────

tab_trends, tab_explore, tab_summary = st.tabs(["📈 Trends", "🔍 Explore", "📊 Summary"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — TRENDS  (uses filtered)
# ══════════════════════════════════════════════════════════════════════════════

with tab_trends:
    gran = st.radio("Granularity", ["Weekly", "Monthly", "Yearly"], horizontal=True, key="gran")

    dfv = filtered.dropna(subset=["date"]).copy()
    if gran == "Weekly":
        dfv["period"] = dfv["date"].dt.strftime("%G-W%V")
    elif gran == "Monthly":
        dfv["period"] = dfv["date"].dt.strftime("%Y-%m")
    else:
        dfv["period"] = dfv["date"].dt.strftime("%Y")

    if dfv.empty:
        st.info("No data matches the current filters.")
    else:
        col_l, col_r = st.columns(2)

        with col_l:
            st.subheader("Price / SqFt Over Time")
            psf_trend = (
                dfv.dropna(subset=["price_psf"])
                .groupby("period")
                .agg(avg_psf=("price_psf", "mean"), median_psf=("price_psf", "median"))
                .reset_index().sort_values("period")
            )
            fig1 = px.line(
                psf_trend, x="period", y=["avg_psf", "median_psf"],
                labels={"value": "$/SqFt", "period": gran, "variable": ""},
                color_discrete_map={"avg_psf": "#636EFA", "median_psf": "#EF553B"},
            )
            fig1.for_each_trace(lambda t: t.update(
                name={"avg_psf": "Avg", "median_psf": "Median"}[t.name]
            ))
            st.plotly_chart(fig1, use_container_width=True)

        with col_r:
            st.subheader("Listing Volume by Status")
            vol_trend = (
                dfv.groupby(["period", "status"])
                .size().reset_index(name="count").sort_values("period")
            )
            fig2 = px.bar(
                vol_trend, x="period", y="count", color="status", barmode="stack",
                labels={"count": "Listings", "period": gran, "status": "Status"},
            )
            st.plotly_chart(fig2, use_container_width=True)

        st.subheader("Avg Price Over Time")
        price_trend = (
            dfv.dropna(subset=["price"])
            .groupby("period")
            .agg(avg_price=("price", "mean"), median_price=("price", "median"))
            .reset_index().sort_values("period")
        )
        fig3 = px.line(
            price_trend, x="period", y=["avg_price", "median_price"],
            labels={"value": "Price ($)", "period": gran, "variable": ""},
            color_discrete_map={"avg_price": "#00CC96", "median_price": "#AB63FA"},
        )
        fig3.for_each_trace(lambda t: t.update(
            name={"avg_price": "Avg", "median_price": "Median"}[t.name]
        ))
        st.plotly_chart(fig3, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — EXPLORE  (uses filtered — no duplicate filter UI)
# ══════════════════════════════════════════════════════════════════════════════

with tab_explore:
    display = filtered[[
        "date", "address", "city", "zip", "status", "property_type",
        "price", "beds", "baths", "sqft", "price_psf",
        "top_rating", "elementary_rating", "middle_rating", "high_rating",
        "hoa", "price_drops", "total_drop", "days_on_market", "url",
    ]].sort_values("date", ascending=False).copy()

    display["price"]     = display["price"].apply(
        lambda x: f"${x:,.0f}" if pd.notna(x) else "")
    display["price_psf"] = display["price_psf"].apply(
        lambda x: f"${x:,.0f}" if pd.notna(x) else "")
    display["total_drop"]= display["total_drop"].apply(
        lambda x: f"-${x:,.0f}" if pd.notna(x) and x > 0 else "")
    display["date"]      = display["date"].dt.strftime("%Y-%m-%d")
    for rc in ["top_rating", "elementary_rating", "middle_rating", "high_rating"]:
        display[rc] = display[rc].apply(lambda x: f"{x:.0f}/10" if pd.notna(x) else "")
    for nc in ["beds", "baths"]:
        display[nc] = display[nc].apply(lambda x: f"{x:g}" if pd.notna(x) else "")

    st.dataframe(
        display,
        use_container_width=True,
        column_config={
            "url":               st.column_config.LinkColumn("URL", display_text="View"),
            "date":              "Date",
            "address":           "Address",
            "city":              "City",
            "zip":               "Zip",
            "status":            "Status",
            "property_type":     "Type",
            "price":             "Price",
            "beds":              "Beds",
            "baths":             "Baths",
            "sqft":              "SqFt",
            "price_psf":         "$/SqFt",
            "top_rating":        "Top School",
            "elementary_rating": "Elem",
            "middle_rating":     "Middle",
            "high_rating":       "High",
            "hoa":               "HOA/mo",
            "price_drops":       "# Drops",
            "total_drop":        "Total Drop",
            "days_on_market":    "DOM",
        },
        hide_index=True,
    )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — SUMMARY  (uses filtered)
# ══════════════════════════════════════════════════════════════════════════════

with tab_summary:
    s1, s2 = st.columns(2)

    with s1:
        st.subheader("By City")
        city_agg = (
            filtered.groupby("city")
            .agg(
                listings=("address", "count"),
                avg_psf=("price_psf", "mean"),
                median_psf=("price_psf", "median"),
                avg_price=("price", "mean"),
                _price_sum=("price", "sum"),
                _sqft_sum=("sqft", "sum"),
                avg_top_school=("top_rating", "mean"),
                price_drops=("price_drops", "sum"),
            )
            .reset_index().sort_values("listings", ascending=False)
        )
        city_agg["wtd_psf"] = (city_agg["_price_sum"] / city_agg["_sqft_sum"]).round(0).astype("Int64")
        city_agg = city_agg.drop(columns=["_price_sum", "_sqft_sum"])
        for col in ["avg_psf", "median_psf", "avg_price"]:
            city_agg[col] = city_agg[col].round(0).astype("Int64")
        city_agg["avg_top_school"] = city_agg["avg_top_school"].round(1)
        st.dataframe(city_agg, use_container_width=True, hide_index=True,
                     column_config={
                         "city": "City", "listings": "Listings",
                         "avg_psf": "Avg $/SqFt", "median_psf": "Median $/SqFt",
                         "wtd_psf": "Wtd $/SqFt",
                         "avg_price": "Avg Price", "avg_top_school": "Avg Top School",
                         "price_drops": "Price Drops",
                     })

    with s2:
        st.subheader("By Status")
        status_agg = (
            filtered.groupby("status")
            .agg(
                listings=("address", "count"),
                avg_psf=("price_psf", "mean"),
                avg_price=("price", "mean"),
                most_recent=("date", "max"),
            )
            .reset_index().sort_values("listings", ascending=False)
        )
        status_agg["avg_psf"]     = status_agg["avg_psf"].round(0).astype("Int64")
        status_agg["avg_price"]   = status_agg["avg_price"].round(0).astype("Int64")
        status_agg["most_recent"] = status_agg["most_recent"].dt.strftime("%Y-%m-%d")
        st.dataframe(status_agg, use_container_width=True, hide_index=True,
                     column_config={
                         "status": "Status", "listings": "Listings",
                         "avg_psf": "Avg $/SqFt", "avg_price": "Avg Price",
                         "most_recent": "Most Recent",
                     })

    st.subheader("By Zip Code")
    zip_agg = (
        filtered.groupby(["zip", "city"])
        .agg(
            listings=("address", "count"),
            avg_psf=("price_psf", "mean"),
            avg_price=("price", "mean"),
            _price_sum=("price", "sum"),
            _sqft_sum=("sqft", "sum"),
            avg_top_school=("top_rating", "mean"),
        )
        .reset_index().sort_values("listings", ascending=False)
    )
    zip_agg["wtd_psf"] = (zip_agg["_price_sum"] / zip_agg["_sqft_sum"]).round(0).astype("Int64")
    zip_agg = zip_agg.drop(columns=["_price_sum", "_sqft_sum"])
    zip_agg["avg_psf"]        = zip_agg["avg_psf"].round(0).astype("Int64")
    zip_agg["avg_price"]      = zip_agg["avg_price"].round(0).astype("Int64")
    zip_agg["avg_top_school"] = zip_agg["avg_top_school"].round(1)
    st.dataframe(zip_agg, use_container_width=True, hide_index=True,
                 column_config={
                     "zip": "Zip", "city": "City", "listings": "Listings",
                     "avg_psf": "Avg $/SqFt", "wtd_psf": "Wtd $/SqFt",
                     "avg_price": "Avg Price", "avg_top_school": "Avg Top School",
                 })

    st.subheader("By School Rating Band")
    bins   = [0, 4.99, 6.99, 8.99, 10]
    labels = ["1-4 (Below Avg)", "5-6 (Average)", "7-8 (Above Avg)", "9-10 (Excellent)"]
    dfr = filtered.copy()
    dfr["rating_band"] = pd.cut(dfr["top_rating"], bins=bins, labels=labels, right=True)
    no_rating = dfr[dfr["top_rating"].isna()].assign(rating_band="No Rating")
    dfr = pd.concat([dfr[dfr["top_rating"].notna()], no_rating])
    band_agg = (
        dfr.groupby("rating_band", observed=False)
        .agg(listings=("address", "count"),
             avg_psf=("price_psf", "mean"),
             avg_price=("price", "mean"))
        .reset_index()
    )
    band_agg["avg_psf"]   = band_agg["avg_psf"].round(0).astype("Int64")
    band_agg["avg_price"] = band_agg["avg_price"].round(0).astype("Int64")
    st.dataframe(band_agg, use_container_width=True, hide_index=True,
                 column_config={
                     "rating_band": "Rating Band", "listings": "Listings",
                     "avg_psf": "Avg $/SqFt", "avg_price": "Avg Price",
                 })

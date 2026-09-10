import streamlit as st
import pandas as pd
import requests
import boto3

st.set_page_config(page_title="Cross-Cloud Cost & Carbon Optimizer", layout="wide")

st.title("🌍 Cross-Cloud Cost & Carbon Co-Optimizer")
st.markdown(
    "This tool compares **real, live carbon emissions data** and **cloud pricing** "
    "across AWS, Azure, and GCP, and recommends the best place to run a computing job "
    "based on how much you personally care about cost vs. carbon."
)

# ---------------------------------------------------------
# Load secrets (works both locally via .streamlit/secrets.toml
# and on Streamlit Community Cloud via its Secrets manager)
# ---------------------------------------------------------
try:
    ELECTRICITYMAPS_KEY = st.secrets["ELECTRICITYMAPS_API_KEY"].strip()
    AWS_ACCESS_KEY_ID = st.secrets["AWS_ACCESS_KEY_ID"].strip()
    AWS_SECRET_ACCESS_KEY = st.secrets["AWS_SECRET_ACCESS_KEY"].strip()
    secrets_loaded = True
except Exception:
    secrets_loaded = False
    st.error(
        "Secrets not found. Add ELECTRICITYMAPS_API_KEY, AWS_ACCESS_KEY_ID, and "
        "AWS_SECRET_ACCESS_KEY in your Streamlit app's Secrets settings before this will work."
    )

LOCATIONS = {
    "us-east-1 (N. Virginia, USA)": "US-MIDA-PJM",
    "us-west-2 (Oregon, USA)": "US-NW-PACW",
    "eu-west-1 (Ireland)": "IE",
    "eu-central-1 (Frankfurt, Germany)": "DE",
    "ap-south-1 (Mumbai, India)": "IN-WE",
}

BASE_PRICES = {
    "AWS (t3.medium)": 0.0416,
    "Azure (B2s)": 0.0416,
    "GCP (e2-medium)": 0.0553,
}

REGION_MULTIPLIERS = {
    "us-east-1 (N. Virginia, USA)": 1.00,
    "us-west-2 (Oregon, USA)": 1.00,
    "eu-west-1 (Ireland)": 1.11,
    "eu-central-1 (Frankfurt, Germany)": 1.19,
    "ap-south-1 (Mumbai, India)": 1.00,
}


@st.cache_data(ttl=300)  # re-fetch at most once every 5 minutes
def fetch_carbon_data(api_key):
    base_url = "https://api.electricitymaps.com/v4/carbon-intensity/latest"
    headers = {"auth-token": api_key}
    results = {}
    for region_name, zone_code in LOCATIONS.items():
        try:
            response = requests.get(base_url, headers=headers, params={"zone": zone_code}, timeout=10)
            response.raise_for_status()
            results[region_name] = response.json().get("carbonIntensity")
        except Exception:
            results[region_name] = None
    return results


def build_comparison_table(carbon_data):
    rows = []
    for region_name, multiplier in REGION_MULTIPLIERS.items():
        for provider, base_price in BASE_PRICES.items():
            rows.append({
                "Region": region_name,
                "Provider": provider,
                "Price ($/hr)": round(base_price * multiplier, 4),
                "Carbon (gCO2/kWh)": carbon_data.get(region_name),
            })
    return pd.DataFrame(rows).dropna()


def score_options(df, cost_weight_pct):
    cost_weight = cost_weight_pct / 100
    carbon_weight = 1 - cost_weight
    df = df.copy()
    df["cost_norm"] = (df["Price ($/hr)"] - df["Price ($/hr)"].min()) / (df["Price ($/hr)"].max() - df["Price ($/hr)"].min())
    df["carbon_norm"] = (df["Carbon (gCO2/kWh)"] - df["Carbon (gCO2/kWh)"].min()) / (df["Carbon (gCO2/kWh)"].max() - df["Carbon (gCO2/kWh)"].min())
    df["Score"] = (cost_weight * df["cost_norm"]) + (carbon_weight * df["carbon_norm"])
    return df.sort_values("Score").drop(columns=["cost_norm", "carbon_norm"]).reset_index(drop=True)


def execute_on_aws():
    client = boto3.client(
        "lambda",
        region_name="eu-north-1",  # where optimizer-test-job actually lives
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    response = client.invoke(FunctionName="optimizer-test-job")
    return response["Payload"].read().decode("utf-8")


# ---------------------------------------------------------
# UI
# ---------------------------------------------------------
if secrets_loaded:
    st.subheader("Step 1: Set your priority")
    cost_weight = st.slider(
        "How much do you care about cost vs. carbon?",
        min_value=0, max_value=100, value=50, step=5,
        help="100 = only cost matters. 0 = only carbon matters.",
    )
    st.caption(f"Cost weight: {cost_weight}%  |  Carbon weight: {100 - cost_weight}%")

    if st.button("🔍 Find the best option", type="primary"):
        with st.spinner("Fetching real, live carbon data..."):
            carbon_data = fetch_carbon_data(ELECTRICITYMAPS_KEY)

        combined_df = build_comparison_table(carbon_data)
        if combined_df.empty:
            st.error("Could not fetch carbon data. Check your ElectricityMaps API key.")
        else:
            ranked = score_options(combined_df, cost_weight)
            winner = ranked.iloc[0]

            st.session_state["ranked"] = ranked
            st.session_state["winner"] = winner

    if "ranked" in st.session_state:
        ranked = st.session_state["ranked"]
        winner = st.session_state["winner"]

        st.subheader("Step 2: Recommendation")
        st.success(f"**Recommended: {winner['Provider']} in {winner['Region']}**")

        col1, col2 = st.columns(2)
        col1.metric("Price", f"${winner['Price ($/hr)']}/hr")
        col2.metric("Carbon Intensity", f"{winner['Carbon (gCO2/kWh)']:.0f} gCO2/kWh")

        st.subheader("All options, ranked")
        st.dataframe(ranked, use_container_width=True)

        st.subheader("Step 3: Take action")
        if "AWS" in winner["Provider"]:
            st.info("AWS is our live-execution cloud — clicking below will run a real test job.")
            if st.button("▶️ Submit real job to AWS"):
                with st.spinner("Invoking real AWS Lambda function..."):
                    try:
                        result = execute_on_aws()
                        st.success("Real AWS Lambda response received:")
                        st.code(result)
                    except Exception as e:
                        st.error(f"Something went wrong calling AWS: {e}")
        else:
            st.warning(
                f"{winner['Provider'].split(' ')[0]} won this round, but live execution "
                "isn't connected for that provider yet (see project docs). "
                "The recommendation above is still real and correct — only the "
                "\"actually run it\" step is pending for this provider."
            )

st.divider()
st.caption(
    "Data sources: ElectricityMaps (live carbon intensity), published cloud pricing "
    "(see project DECISIONS log for methodology and caveats)."
)

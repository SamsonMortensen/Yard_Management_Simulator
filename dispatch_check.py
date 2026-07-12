from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from config import YARD_CAPACITY, get_table, scan_all

#Page Config
st.set_page_config(page_title="Yard Operations", layout="wide")
st.title("Real-Time Yard Management Dashboard")

#Connect to AWS
table = get_table()

@st.cache_data(ttl=5)
def load_data():
    return pd.DataFrame(scan_all(table))

if st.button("Refresh"):
    load_data.clear()

df = load_data()

if df.empty:
    st.info("The yard is currently empty. Run main.py to generate traffic.")
    st.stop()

#Departed units keep their history but no longer occupy the yard
in_yard = df[df['Current_Status'] != 'Departed'].copy()
departed = df[df['Current_Status'] == 'Departed']

st.subheader("Live Yard Metrics")
col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("Containers in Yard", len(in_yard))
with col2:
    utilization = (len(in_yard) / YARD_CAPACITY) * 100
    st.metric("Yard Capacity Utilization", f"{utilization:.3f}%")
with col3:
    gate_hold = len(in_yard[in_yard['Current_Status'] == 'Ingate_Hold'])
    st.metric("Units Holding at Gate", gate_hold)
with col4:
    if departed.empty:
        st.metric("Avg Dwell of Departed Units", "—")
    else:
        avg_dwell = departed['Dwell_Time_Hours'].astype(float).mean()
        st.metric("Avg Dwell of Departed Units", f"{avg_dwell:.2f} hrs")

st.markdown("----------")
st.subheader("Active Inventory Roster")

if in_yard.empty:
    st.caption("No containers currently in the yard.")
else:
    #Live dwell, computed on the fly from arrival time
    now = datetime.now(timezone.utc)
    arrivals = pd.to_datetime(in_yard['Arrival_Time'], utc=True)
    in_yard['Hours_In_Yard'] = ((now - arrivals).dt.total_seconds() / 3600).round(2)

    # Reorder the columns (Parked_By_Employee stays internal — audit data, not public view)
    in_yard = in_yard[['Container_ID', 'Assigned_Spot', 'Equipment_Type',
                       'Current_Status', 'Arrival_Time', 'Hours_In_Yard']]
    st.dataframe(in_yard, use_container_width=True, hide_index=True)

st.subheader("Departure Log")

if departed.empty:
    st.caption("No departures logged yet.")
else:
    departed_view = departed[['Container_ID', 'Equipment_Type',
                              'Arrival_Time', 'Dwell_Time_Hours']]
    st.dataframe(departed_view, use_container_width=True, hide_index=True)

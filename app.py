import streamlit as st
import boto3
import pandas as pd

#Page Config
st.set_page_config(page_title="Yard Operations", layout="wide")
st.title("Real-Time Yard Management Dashboard")

#Connect to AWS
dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
table = dynamodb.Table('Yard_Inventory_Sim')

#Function
def load_data():
    response = table.scan()  #Scans the table
    items = response.get('Items', [])
    return pd.DataFrame(items)

df = load_data()

# Build the User Interface
if not df.empty:
    st.subheader("Live Yard Metrics")
    
    # Create columns
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Total Containers in Yard", len(df))
    with col2:
        utilization = (len(df) / 4000) * 100
        st.metric("Yard Capacity Utilization", f"{utilization:.3f}%")
    with col3:
        # Count how many are at the gate
        gate_hold = len(df[df['Current_Status'] == 'Ingate_Hold'])
        st.metric("Units Holding at Gate", gate_hold)

    st.markdown("----------")
    st.subheader("Active Inventory Roster")
    
    # Reorder the columns
    df = df[['Container_ID', 'Assigned_Spot', 'Equipment_Type', 'Current_Status', 'Arrival_Time']]
    
    # Display
    st.dataframe(df, use_container_width=True, hide_index=True)
    
else:
    st.info("The yard is currently empty. Run your simulator script to generate traffic")
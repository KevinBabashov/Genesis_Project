"""
Data Preprocessing Module for LiftOFF TMF Milling Bayesian Optimization (Model A)

This script loads, cleans, aggregates, and processes raw particle morphology CSV files
from planetary ball-milling experiments into batch-level summary metrics.
"""

import os
import glob
import re
from anyio import Path
import pandas as pd
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "Data", "raw_data"))
DEFAULT_OUTPUT_CSV = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "Data", "milling_summary_processed.csv"))


def parse_condition_string(condition_str):
    """
    Parses condition string formatted as: [YYMMDD]_[RPM]_[Time_ID]_[Magnification]
    E.g., '261006_900_4_30000' -> Date: 261006, RPM: 900, Step/Hours: 4, Mag: 30000
    """
    parts = str(condition_str).split('_')
    if len(parts) >= 4:
        date = parts[0]
        try:
            rpm = float(parts[1])
        except ValueError:
            rpm = 900.0
        try:
            time_step = float(parts[2])
        except ValueError:
            time_step = 1.0
        try:
            mag = float(parts[3])
        except ValueError:
            mag = 30000.0
        return {
            'batch_date': date,
            'rpm': rpm,
            'milling_time_h': time_step,
            'magnification': mag
        }
    else:
        return {
            'batch_date': 'unknown',
            'rpm': 900.0,
            'milling_time_h': 1.0,
            'magnification': 30000.0
        }


def calculate_milling_energy(rpm, time_h, energy_per_hour_kj_g=1.29):
    """
    Calculates cumulative specific milling energy dose E_cum (kJ/g).
    E_cum = n_cycles * 1.29 kJ/g (or C * N^3 * t model from Burgio et al. / Jafter et al.).
    Here, E_cum scales linearly with milling time step for a fixed RPM (900 rpm).
    """
    # Standardizing to kJ/g based on Burgio/Jafter impact model derivation
    # E_cum = C * N^3 * t or n_cyc * 1.29 kJ/g
    return time_h * energy_per_hour_kj_g * ((rpm / 900.0) ** 3)


def load_raw_milling_data(data_dir="."):
    """
    Loads all particle CSV files from the specified directory.
    """
    csv_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in directory '{data_dir}'")
    
    dfs = []
    for f in csv_files:
        try:
            df = pd.read_csv(f)
            df["source_file"] = os.path.basename(f)
            dfs.append(df)
        except Exception as e:
            print(f"Warning: Could not read {f}: {e}")
            
    combined_df = pd.concat(dfs, ignore_index=True)
    return combined_df


def clean_particle_data(combined_df, filter_border=True):
    """
    Cleans raw particle data:
    - Filters out particles touching image border if filter_border=True
    - Converts ECD (um) to ECD (nm)
    - Removes invalid or non-positive area/ECD rows
    """
    df = combined_df.copy()
    
    if filter_border and 'Touches Border' in df.columns:
        df = df[df['Touches Border'] == False]
        
    df = df[df['ECD (um)'] > 0]
    df['ECD_nm'] = df['ECD (um)'] * 1000.0
    
    return df


def aggregate_batch_metrics(cleaned_df):
    """
    Aggregates particle measurements by experimental condition into batch-level statistics.
    Computes D10, D50 (median), D90, Span, Aspect Ratio, Circularity, and E_cum dose.
    """
    summary_rows = []
    
    for condition, group in cleaned_df.groupby('Condition'):
        meta = parse_condition_string(condition)
        ecds = group['ECD_nm'].values
        
        d10 = float(np.percentile(ecds, 10))
        d50 = float(np.median(ecds))
        d90 = float(np.percentile(ecds, 90))
        span = (d90 - d10) / d50 if d50 > 0 else np.nan
        
        e_cum = calculate_milling_energy(meta['rpm'], meta['milling_time_h'])
        
        summary_rows.append({
            'Condition': condition,
            'Batch_Date': meta['batch_date'],
            'RPM': meta['rpm'],
            'Milling_Time_h': meta['milling_time_h'],
            'E_cum_kJ_g': round(e_cum, 3),
            'Valid_Particle_Count': len(group),
            'D10_nm': round(d10, 2),
            'D50_nm': round(d50, 2),
            'D90_nm': round(d90, 2),
            'Span': round(span, 3),
            'Mean_Aspect_Ratio': round(float(group['Aspect Ratio'].mean()), 3),
            'Std_Aspect_Ratio': round(float(group['Aspect Ratio'].std()), 3),
            'Mean_Circularity': round(float(group['Circularity'].mean()), 3),
            'Std_Circularity': round(float(group['Circularity'].std()), 3),
            'Magnification': int(group['Magnification'].iloc[0]) if 'Magnification' in group.columns else int(meta['magnification'])
        })
        
    summary_df = pd.DataFrame(summary_rows)
    summary_df = summary_df.sort_values(by=['E_cum_kJ_g', 'Condition']).reset_index(drop=True)
    return summary_df


def process_pipeline(data_dir=DEFAULT_DATA_DIR, output_csv=DEFAULT_OUTPUT_CSV): 
    print("Loading raw milling CSVs...")
    raw_df = load_raw_milling_data(data_dir)
    print(f"Loaded {len(raw_df)} total particle records.")
    
    print("Cleaning particle measurements...")
    clean_df = clean_particle_data(raw_df, filter_border=True)
    print(f"Retained {len(clean_df)} valid non-border particles.")
    
    print("Aggregating batch-level metrics...")
    summary_df = aggregate_batch_metrics(clean_df)
    
    summary_df.to_csv(output_csv, index=False)
    print(f"Successfully exported summary data to '{output_csv}'.")
    return summary_df


if __name__ == "__main__":
    process_pipeline()

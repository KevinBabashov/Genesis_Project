"""Particle summaries joined to sample-specific cycles in 00_Package_Summary.xlsx.

Filename numbers identify samples. Nominal
energy is 1.29 kJ/g per cycle at 900 rpm. Particle D50 is number-weighted.
"""
from pathlib import Path
import re
import posixpath
import zipfile
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent


def find_project_root(start=SCRIPT_DIR):
    for folder in (Path(start).resolve(), *Path(start).resolve().parents):
        if (folder / "Data" / "raw_data").is_dir():
            return folder
    raise FileNotFoundError("Cannot locate Data/raw_data above this folder.")


def parse_condition_string(condition_str):
    match = re.fullmatch(r"(\d{6})_(\d+(?:\.\d+)?)_(\d+(?:\.\d+)?)_(\d+)", str(condition_str))
    if match is None:
        raise ValueError(f"Invalid Condition: {condition_str!r}; expected date_rpm_step_magnification")
    date, rpm, step, mag = match.groups()
    pd.to_datetime(date, format="%y%m%d", errors="raise")
    rpm, step, mag = float(rpm), float(step), int(mag)
    if rpm <= 0 or step <= 0 or mag <= 0:
        raise ValueError(f"Non-positive metadata in {condition_str!r}")
    return dict(batch_date=date, rpm=rpm, sample_id=step, magnification=mag)


def calculate_milling_energy(rpm, n_cycles, energy_per_cycle_kj_g=1.29, reference_rpm=900.0):
    """Nominal kJ/g, assuming unchanged net cycle duration and mill/recipe.

    RPM-cubed scaling follows the milling energy derivation.
    This is nominal ball energy, not measured energy absorbed by the powder.
    """
    values = np.asarray([rpm, n_cycles, energy_per_cycle_kj_g, reference_rpm], dtype=float)
    if not np.isfinite(values).all() or rpm <= 0 or n_cycles < 0 or energy_per_cycle_kj_g <= 0 or reference_rpm <= 0:
        raise ValueError("Energy calculation requires finite, physically valid values.")
    return n_cycles * energy_per_cycle_kj_g * (rpm / reference_rpm) ** 3


def load_raw_milling_data(data_dir=None):
    data_dir = Path(data_dir) if data_dir is not None else find_project_root() / "Data/raw_data"
    files = sorted(data_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files in {data_dir}")
    required = {"Condition", "ECD (um)", "Area (um2)", "Touches Border", "Aspect Ratio", "Circularity"}
    frames = []
    for path in files:
        frame = pd.read_csv(path)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
        if frame.empty or frame.Condition.isna().any() or frame.Condition.nunique() != 1:
            raise ValueError(f"{path.name}: expected one nonempty Condition")
        meta = parse_condition_string(frame.Condition.iloc[0])
        if not path.stem.startswith(str(frame.Condition.iloc[0]) + "_"):
            raise ValueError(f"{path.name}: filename/Condition mismatch")
        if "Magnification" in frame:
            mag = pd.to_numeric(frame.Magnification, errors="raise")
            if not mag.eq(meta["magnification"]).all():
                raise ValueError(f"{path.name}: magnification disagrees with Condition")
        frame["source_file"] = path.name
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def clean_particle_data(combined_df, filter_border=True):
    df = combined_df.copy()
    # Normalize boolean representations and reject missing or unknown flags.
    border = df["Touches Border"].astype(str).str.strip().str.lower().map(
        {"true": True, "false": False, "1": True, "0": False}
    )
    if border.isna().any():
        raise ValueError("Unrecognized or missing Touches Border flag")
    df["Touches Border"] = border.astype(bool)
    for column in ["ECD (um)", "Area (um2)", "Aspect Ratio", "Circularity"]:
        df[column] = pd.to_numeric(df[column], errors="raise")
    valid = (np.isfinite(df["ECD (um)"]) & (df["ECD (um)"] > 0)
             & np.isfinite(df["Area (um2)"]) & (df["Area (um2)"] > 0))
    if filter_border:
        valid &= ~df["Touches Border"]
    result = df.loc[valid].copy()
    if result.empty:
        raise ValueError("No valid particles remain")
    result["ECD_nm"] = result["ECD (um)"] * 1000.0
    result.attrs["input_records"] = len(df)
    result.attrs["removed_records"] = int((~valid).sum())
    return result


def load_package_metadata(path=None):
    """Read the Conditions sheet using standard-library XLSX XML support.

    No optional Excel engine is needed. Require numeric cached values for
    formula cells. Source sample names, cycles, image counts, energies and
    rights labels are read from the workbook.
    """
    path = Path(path) if path else find_project_root() / 'Documentation/00_Package_Summary.xlsx'
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    relns = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(x.itertext()) for x in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', ns)]
        book = ET.fromstring(archive.read('xl/workbook.xml'))
        sheet = next((s for s in book.findall('s:sheets/s:sheet', ns) if s.attrib['name'] == 'Conditions'), None)
        if sheet is None:
            raise ValueError('Package workbook has no Conditions sheet')
        relationships = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        target = next(r.attrib['Target'] for r in relationships if r.attrib['Id'] == sheet.attrib[relns+'id'])
        member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
        root = ET.fromstring(archive.read(member))
        rows = []
        notices = []
        for row in root.findall('s:sheetData/s:row', ns):
            cells = {}
            for cell in row.findall('s:c', ns):
                column = re.sub(r'\d+', '', cell.attrib['r'])
                value = cell.find('s:v', ns)
                if cell.attrib.get('t') == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//s:t', ns))
                elif value is not None:
                    value = strings[int(value.text)] if cell.attrib.get('t') == 's' else value.text
                else:
                    value = None
                cells[column] = value
            sample = cells.get('A') or ''
            if sample.startswith(('PROTECTED RIGHTS NOTICE', 'LIMITED RIGHTS NOTICE')):
                notices.append(sample)
            if re.fullmatch(r'\d{6}_\d+(?:\.\d+)?_\d+(?:\.\d+)?_\d+', sample):
                meta = parse_condition_string(sample)
                try:
                    cycles, images, dose = (float(cells[k]) for k in ['B','C','D'])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f'{sample}: missing/invalid workbook metadata') from error
                if not np.isfinite([cycles, images, dose]).all() or cycles < 0 or cycles != int(cycles) or images < 1 or images != int(images):
                    raise ValueError(f'{sample}: invalid cycles/image count')
                expected = calculate_milling_energy(meta['rpm'], cycles)
                if not np.isclose(dose, expected, rtol=0, atol=0.005):
                    raise ValueError(f'{sample}: workbook dose {dose} disagrees with per-cycle energy {expected}')
                rows.append(dict(Condition=sample, Cycle_Count=int(cycles), Expected_Image_Count=int(images),
                                 E_cum_kJ_g=dose, Data_Rights=cells.get('E'),
                                 Metadata_Source=f'{path.name}:Conditions!A{row.attrib["r"]}:E{row.attrib["r"]}'))
    result = pd.DataFrame(rows)
    if result.empty or result.Condition.duplicated().any():
        raise ValueError('Package metadata is empty or contains duplicate sample names')
    result.attrs['rights_notices'] = notices
    return result


def aggregate_batch_metrics(cleaned_df, metadata=None, net_minutes_per_cycle=None):
    """Join by full sample name: a sample ID alone is not a valid join key."""
    metadata = load_package_metadata() if metadata is None else metadata
    if metadata.Condition.duplicated().any():
        raise ValueError('Duplicate sample metadata')
    if net_minutes_per_cycle is not None and (not np.isfinite(net_minutes_per_cycle) or net_minutes_per_cycle <= 0):
        raise ValueError('Net cycle duration must be positive and finite')
    lookup = metadata.set_index('Condition')
    rows = []
    for condition, group in cleaned_df.groupby('Condition', sort=True):
        meta = parse_condition_string(condition)
        if condition not in lookup.index:
            raise ValueError(f'Missing workbook metadata for {condition}')
        experiment = lookup.loc[condition]
        d10, d50, d90 = np.percentile(group.ECD_nm.to_numpy(), [10, 50, 90])
        image_medians = group.groupby('source_file').ECD_nm.median()
        rows.append(dict(
            Condition=condition, Batch_Date=meta['batch_date'], RPM=meta['rpm'], Sample_ID=meta['sample_id'],
            Cycle_Count=int(experiment.Cycle_Count), E_cum_kJ_g=float(experiment.E_cum_kJ_g),
            Milling_Time_h=np.nan if net_minutes_per_cycle is None else experiment.Cycle_Count*net_minutes_per_cycle/60,
            Magnification=meta['magnification'], Valid_Particle_Count=len(group), Image_Count=len(image_medians),
            D10_nm=d10, D50_nm=d50, D90_nm=d90, Span=(d90-d10)/d50,
            Image_D50_Min_nm=image_medians.min(), Image_D50_Max_nm=image_medians.max(),
            Mean_Aspect_Ratio=group['Aspect Ratio'].mean(), Std_Aspect_Ratio=group['Aspect Ratio'].std(),
            Mean_Circularity=group.Circularity.mean(), Std_Circularity=group.Circularity.std(),
            Data_Rights=experiment.Data_Rights, Metadata_Source=experiment.Metadata_Source))
    return pd.DataFrame(rows).sort_values(['E_cum_kJ_g','Condition']).reset_index(drop=True)


def process_pipeline(data_dir=None, output_csv=None, *, metadata_path=None, net_minutes_per_cycle=None):
    raw = load_raw_milling_data(data_dir)
    metadata = load_package_metadata(metadata_path)
    if set(raw.Condition) != set(metadata.Condition):
        raise ValueError('Raw samples and workbook samples differ: ' + str(set(raw.Condition) ^ set(metadata.Condition)))
    actual_images = raw.groupby('Condition').source_file.nunique()
    expected_images = metadata.set_index('Condition').Expected_Image_Count
    if not actual_images.eq(expected_images.reindex(actual_images.index)).all():
        raise ValueError('Image counts do not match the package workbook')
    clean = clean_particle_data(raw)
    lost = set(raw.Condition) - set(clean.Condition)
    if lost:
        raise ValueError(f'Conditions with no valid particles: {sorted(lost)}')
    summary = aggregate_batch_metrics(clean, metadata, net_minutes_per_cycle)
    print(f'{len(raw):,} particles -> {len(clean):,} valid particles; {len(summary)} samples, '
          f'{summary.E_cum_kJ_g.nunique()} energy doses')
    print('Cycle counts and doses joined by full sample name from Conditions sheet.')
    if output_csv is not None:
        output_csv = Path(output_csv)
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(output_csv, index=False)
        output_csv.with_suffix('.notices.txt').write_text('Source: Documentation/00_Package_Summary.xlsx\n\n' + '\n\n'.join(metadata.attrs['rights_notices']) + '\n')
    return summary


if __name__ == '__main__':
    process_pipeline(output_csv=find_project_root() / 'Data/milling_summary_processed.csv')

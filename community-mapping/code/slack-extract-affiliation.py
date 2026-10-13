# This script reads in a csv file of slack member export and returns the affiliations of members based on their email, with a number at each institution
# Adapted from https://github.com/rcmcooperative/community-mapping/blob/main/code/sharepoint-to-kumu-xlsx.py
# Gould van Praag, C. (2025). RCM Cooperative Community Mapping Workflow. Zenodo. https://doi.org/10.5281/zenodo.15320348

# # Set-up in terminal
# Adapted from [TTW local build step-by-step guide](https://the-turing-way.netlify.app/community-handbook/local-build.html?highlight=conda%20env) to use a conda environment
# 1. [install miniconda](https://docs.conda.io/projects/miniconda/en/latest/)
# 2. `conda init`
# 3. `curl https://github.com/rcmcooperative/community-mapping/blob/main/DO-NOT-USE/kumu-env.yml` > kumu-env.yml
# 4. `conda env create -f kumu-env.yml`
# 5. `conda activate kumu_env`
# 6. in vs code, set python interpreter to kumu_env before running debugging (see [guide here](https://code.visualstudio.com/docs/python/environments#_using-the-create-environment-command))




# library of functions for checking, making, renaming files etc.
import os as os
import subprocess
import pandas as pd
import numpy as np
import csv
# from anonymizedf.anonymizedf import anonymize
import time
import json
from datetime import datetime
import tldextract

# where all our data is (downloaded from slack worksapce by admin)
dataRoot = '/Users/cassandragouldvanpraag/Library/Mobile Documents/com~apple~CloudDocs/repos/rcmcooperative/partner-UKTRE/private/'

# Define filenames
data_in = 'slack-uktrecommunity-members.csv'

# adding a timestamp to save overwrites when we do multiple runs of the output
timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
data_out = f'slack-affiliation-counts-{timestamp}.xlsx'

# Use join to define paths
path_in = os.path.join(dataRoot, data_in)
path_out = os.path.join(dataRoot, data_out)

# Load the CSV
df = pd.read_csv(path_in)

# 1. Extract domain-email and clean up
df = df.dropna(subset=['email'])
df['domain-email'] = df['email'].str.split('@').str[1].str.lower()

# 2. Flag: domain-personal (1 if in list, else 0)
personal_providers = [
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 
    'live.com', 'msn.com', 'icloud.com', 'me.com', 'yahoo.com', 
    'ymail.com', 'btinternet.com', 'virginmedia.com', 'protonmail.com'
]
df['domain-personal'] = df['domain-email'].apply(lambda x: 1 if x in personal_providers else 0)

# 3. Flag: domaine-academic (1 if ends in .ac.uk, else 0)
df['domaine-academic'] = df['domain-email'].apply(lambda x: 1 if x.endswith('.ac.uk') else 0)

# 4. Flag: domain-nhs (1 if "nhs" in string, else 0)
df['domain-nhs'] = df['domain-email'].apply(lambda x: 1 if 'nhs' in x else 0)

# 5. Flag: domain-gov (1 if ends in .gov.uk or .gov.scot, else 0)
df['domain-gov'] = df['domain-email'].apply(lambda x: 1 if x.endswith(('.gov.uk', '.gov.scot')) else 0)

# 6. Extract core affiliation name
df['affiliation-clean'] = df['domain-email'].apply(lambda x: tldextract.extract(x).domain)

# 7. Aggregate data to maintain anonymity
# Grouping by all logic columns to create a unique mapping for the report
group_cols = ['affiliation-clean', 'domain-email', 'domain-personal', 'domaine-academic', 'domain-nhs', 'domain-gov']
counts = df.groupby(group_cols).size().reset_index(name='Count')

# Sort by count descending so the most common affiliations are at the top
counts = counts.sort_values(by='Count', ascending=False)

# 8. Report out
counts.to_excel(path_out, index=False)

print(f"Success! Anonymous report with Government flags saved to {path_out}")
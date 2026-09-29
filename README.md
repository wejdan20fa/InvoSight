
# InvoSight
### AI-Powered Invoice Intelligence

InvoSight is an AI-powered invoice processing and verification system designed to transform invoice documents into structured, actionable financial information.

The system combines deep learning, optical character recognition (OCR), automated validation, and an interactive dashboard to support efficient invoice processing and review.

## Key Features

- **AI-Powered Extraction:** Extracts essential information from invoice images.
- **Automated Verification:** Validates extracted values using available document evidence and calculation checks.
- **Discrepancy Detection:** Identifies fields that may require further review.
- **Interactive Editing:** Allows users to correct extracted values and recalculate related amounts.
- **Validation Summary:** Displays verification results and the status of individual fields.
- **Invoice History:** Provides access to previously processed invoices.
- **Database Management:** Stores and manages saved invoice records.
- **JSON Export:** Downloads extracted information in a structured format.
- **Verification Reports:** Generates professional PDF reports with verification results and recommendations.

## Extracted Invoice Fields

InvoSight processes nine essential fields:

1. Invoice Number
2. Invoice Date
3. Due Date
4. Vendor Name
5. Customer Name
6. Subtotal
7. Discount
8. Tax Amount
9. Total Amount

## Technology Stack

- **Language:** Python
- **Framework:** Streamlit
- **Deep Learning:** PyTorch and Hugging Face Transformers
- **Document Understanding:** Fine-tuned Donut model
- **OCR Verification:** Tesseract OCR
- **Database:** SQLite
- **Report Generation:** ReportLab
- **Image Processing:** Pillow

## How It Works

1. Upload an invoice image.
2. Extract relevant invoice information.
3. Verify extracted fields using available evidence and calculation rules.
4. Review flagged fields and make corrections when necessary.
5. Save invoice records and export structured data.
6. Generate a verification report with recommendations.

## Verification Status

**Validated:** All required fields passed the available verification checks.

**Needs Review:** One or more fields require further review or independent verification.

**Modified:** One or more extracted values have been changed by the user. Modified values may require additional verification.

## Running Locally

### Prerequisites

- Python 3.12
- Tesseract OCR
- Required Python dependencies
- Locally available trained model files

### Installation

Install the required Python packages:

```bash
pip install -r requirements.txt
```

Install Tesseract OCR and ensure the executable is accessible.

Place the trained model files inside the configured model directory.

### Start the Application

```bash
streamlit run Dashborad.py
```

Open the local URL displayed in the terminal.

## Project Structure

```text
InvoSight/
├── Dashborad.py
├── Database.py
├── Dount_backend.py
├── ocr_evidence.py
├── validation_rules.py
├── report_generator.py
├── InvoSight_Assets/
├── requirements.txt
├── packages.txt
└── README.md
```

The trained model, local database, environment files, and sensitive data are excluded from the public repository.

## Important Notice

InvoSight provides automated verification support. Its results depend on the available document evidence and validation checks.

Verification results do not constitute final financial or payment approval.

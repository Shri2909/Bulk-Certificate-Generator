# Problem statement (verbatim transcription)

> **The PDF is authoritative:** [`docs/problem_statement.pdf`](problem_statement.pdf). This file is a
> plain-text transcription kept for grep and diff. If the two ever disagree, the PDF wins. Checked
> word-for-word against `pdftotext` output on 2026-10-11. Requirement IDs in `docs/REQUIREMENTS.md` quote this text.

## Bulk Certificate Generator

### Objective
Build a backend API that accepts certificate generation requests for a list of recipients and generates
certificates based on a predefined template.

The system should handle the generation process efficiently and provide a way to track the status and retrieve
the generated certificates.

AI tools are allowed. You should be able to understand, explain, and modify the code you submit.

### Technology
- Python
- One of: FastAPI, Django + Django REST Framework, or Flask
- A relational database

You may use any additional libraries or tools you consider appropriate.

### Problem Statement
An organization wants to generate certificates for a large number of participants after an event or course.

The client should be able to submit a list of recipients along with the certificate information.

The backend should:
1. Accept a certificate generation request.
2. Validate the provided recipient data.
3. Generate a certificate for each valid recipient using a predefined certificate template.
4. Track the generation status.
5. Allow the client to check the progress/result of the request.
6. Allow generated certificates to be retrieved.

The implementation should support bulk generation rather than requiring the client to make one API request
per certificate.

### Certificate Generation
- Use a single predefined certificate template.
- You do not need to build a template editor or support multiple certificate designs.
- The generated certificate should contain the recipient-specific information provided in the request.
- The exact certificate format and generation library are up to you.

### Validation & Failure Handling
The system should handle invalid recipient data appropriately.

A failure while generating one certificate should not unnecessarily prevent other valid certificates in the same
job from being generated.

The job status should provide enough information to identify successful and failed generations.

### Bulk Processing
The API should be designed around the fact that a single request may contain many recipients.

You may choose how the generation is processed. For example, generation may be handled synchronously or
through background processing.

The choice and reasoning should be documented.

### Testing
Include tests covering the important parts of the application. At minimum, test:
- Creating a generation job
- Input validation
- Certificate generation
- Job status/progress
- Handling an individual certificate failure
- Retrieving generated certificates

### Documentation
Include a README explaining:
- How to set up the project
- How to run the application
- How to run tests
- How to submit a certificate generation request
- How to retrieve generated certificates
- Important implementation/design decisions

### Optional
You may add small improvements if you believe they are useful, but they are not required.

Optional features should not come at the cost of completing the required functionality.

### Important Note
During the interview, you may be asked to explain your implementation, justify design decisions, debug or
modify part of the application, or handle a changed requirement.

You should therefore be comfortable explaining and modifying the code you submit.

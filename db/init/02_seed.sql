-- Seed data, copied from the current contents of the 7 non-empty JSON files.
-- access_requests.json is currently [] and gets no rows.

INSERT INTO entitlement_catalog (entitlement_id, entitlement_name, application, owner) VALUES
    ('ENT001', 'SAP_FIN_DISPLAY', 'SAP ECC', 'Finance IT'),
    ('ENT002', 'SAP_AP_INVOICE', 'SAP ECC', 'Finance IT'),
    ('ENT003', 'POWERBI_FINANCE', 'PowerBI', 'BI Team'),
    ('ENT004', 'FIN_SHAREPOINT', 'SharePoint', 'Collaboration Team'),
    ('ENT005', 'JIRA_USER', 'JIRA', 'Engineering IT'),
    ('ENT006', 'GITHUB_DEV', 'GitHub', 'DevOps Team'),
    ('ENT007', 'CONFLUENCE_USER', 'Confluence', 'Engineering IT'),
    ('ENT008', 'RSA_GRC', 'RSA Archer', 'Risk IT'),
    ('ENT009', 'RISK_PORTAL', 'Risk Portal', 'Risk IT'),
    ('ENT010', 'AUDIT_TOOL', 'Audit Platform', 'Audit IT'),
    ('ENT011', 'POWERBI_RISK', 'PowerBI', 'Risk IT'),
    ('ENT012', 'POWERBI_AUDIT', 'PowerBI', 'Audit IT'),
    ('ENT013', 'SHAREPOINT_AUDIT', 'SharePoint', 'Audit IT'),
    ('ENT014', 'SAP_PAYMENT_APPROVER', 'SAP ECC', 'Finance IT'),
    ('ENT015', 'SAP_VENDOR_CREATE', 'SAP ECC', 'Finance IT'),
    ('ENT016', 'AD_DOMAIN_ADMIN', 'Active Directory', 'Infrastructure IT'),
    ('ENT017', 'WORKDAY_HR', 'Workday', 'HR IT'),
    ('ENT018', 'AWS_CONSOLE', 'AWS', 'DevOps Team');

INSERT INTO entitlement_risk_scores (entitlement_name, application, risk_score, risk_category) VALUES
    ('SAP_FIN_DISPLAY', 'SAP ECC', 15, 'Low'),
    ('SAP_AP_INVOICE', 'SAP ECC', 45, 'Medium'),
    ('POWERBI_FINANCE', 'PowerBI', 10, 'Low'),
    ('FIN_SHAREPOINT', 'SharePoint', 8, 'Low'),
    ('JIRA_USER', 'JIRA', 5, 'Low'),
    ('GITHUB_DEV', 'GitHub', 35, 'Medium'),
    ('CONFLUENCE_USER', 'Confluence', 10, 'Low'),
    ('RSA_GRC', 'RSA Archer', 70, 'High'),
    ('POWERBI_RISK', 'PowerBI', 10, 'Low'),
    ('RISK_PORTAL', 'Risk Portal', 40, 'Medium'),
    ('AUDIT_TOOL', 'Audit Platform', 75, 'High'),
    ('POWERBI_AUDIT', 'PowerBI', 10, 'Low'),
    ('SHAREPOINT_AUDIT', 'SharePoint', 10, 'Low'),
    ('SAP_PAYMENT_APPROVER', 'SAP ECC', 95, 'Critical'),
    ('SAP_VENDOR_CREATE', 'SAP ECC', 90, 'Critical'),
    ('AD_DOMAIN_ADMIN', 'Active Directory', 100, 'Critical'),
    ('WORKDAY_HR', 'Workday', 20, 'Low'),
    ('AWS_CONSOLE', 'AWS', 60, 'Medium');

INSERT INTO identities (employee_id, name, department, job_role, job_level, location, manager_id, entitlements) VALUES
    ('EMP001', 'Ramesh', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', '', 'SAP_FIN_DISPLAY;SAP_AP_INVOICE;POWERBI_FINANCE'),
    ('EMP002', 'Sneha', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', 'EMP001', 'SAP_FIN_DISPLAY;SAP_AP_INVOICE;POWERBI_FINANCE'),
    ('EMP003', 'Ajay', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', 'EMP001', 'SAP_FIN_DISPLAY;POWERBI_FINANCE'),
    ('EMP004', 'Manoj', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', 'EMP001', 'SAP_FIN_DISPLAY;SAP_AP_INVOICE;POWERBI_FINANCE'),
    ('EMP005', 'Pooja', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', 'EMP001', 'SAP_FIN_DISPLAY;SAP_AP_INVOICE;POWERBI_FINANCE;FIN_SHAREPOINT'),
    ('EMP006', 'Ravi', 'Technology', 'Software Engineer', 'L2', 'Bangalore', '', 'JIRA_USER;GITHUB_DEV;CONFLUENCE_USER'),
    ('EMP007', 'Sonia', 'Technology', 'Software Engineer', 'L2', 'Bangalore', 'EMP006', 'JIRA_USER;GITHUB_DEV;CONFLUENCE_USER'),
    ('EMP008', 'Akash', 'Technology', 'Software Engineer', 'L2', 'Bangalore', 'EMP006', 'JIRA_USER;GITHUB_DEV'),
    ('EMP009', 'Meera', 'Risk', 'Risk Analyst', 'L3', 'Bangalore', 'EMP010', 'RSA_GRC;POWERBI_RISK;RISK_PORTAL'),
    ('EMP010', 'John', 'Audit', 'Internal Auditor', 'L3', 'Mumbai', '', 'AUDIT_TOOL;POWERBI_AUDIT;SHAREPOINT_AUDIT');

INSERT INTO new_joiners (employee_id, name, department, job_role, job_level, location, manager_id, cost_center, start_date) VALUES
    ('NJ1001', 'Rahul Sharma', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', 'EMP001', 'FIN001', '2026-08-01'),
    ('NJ1002', 'Priya Nair', 'Finance', 'Financial Analyst', 'L2', 'Bangalore', 'EMP001', 'FIN001', '2026-08-01'),
    ('NJ1003', 'Amit Gupta', 'Finance', 'Financial Analyst', 'L2', 'Hyderabad', 'EMP001', 'FIN001', '2026-08-01'),
    ('NJ1004', 'Anjali Rao', 'Technology', 'Software Engineer', 'L2', 'Bangalore', 'EMP002', 'TECH001', '2026-08-01'),
    ('NJ1005', 'Karan Mehta', 'Technology', 'Software Engineer', 'L2', 'Pune', 'EMP002', 'TECH001', '2026-08-01'),
    ('NJ1006', 'Neha Singh', 'Risk', 'Risk Analyst', 'L3', 'Bangalore', 'EMP003', 'RISK001', '2026-08-01'),
    ('NJ1007', 'Suresh Iyer', 'Audit', 'Internal Auditor', 'L3', 'Mumbai', 'EMP004', 'AUD001', '2026-08-01'),
    ('NJ1008', 'Deepa Joseph', 'HR', 'HR Specialist', 'L2', 'Bangalore', 'EMP005', 'HR001', '2026-08-01'),
    ('NJ1009', 'Vivek Kumar', 'Technology', 'Cloud Engineer', 'L3', 'Hyderabad', 'EMP002', 'TECH001', '2026-08-01'),
    ('NJ1010', 'Arjun Patel', 'Finance', 'Senior Financial Analyst', 'L3', 'Bangalore', 'EMP001', 'FIN001', '2026-08-01');

INSERT INTO peer_affinity_scores (job_role, department, entitlement, peer_count, total_peers, affinity_score) VALUES
    ('Financial Analyst', 'Finance', 'SAP_FIN_DISPLAY', 5, 5, 100),
    ('Financial Analyst', 'Finance', 'SAP_AP_INVOICE', 4, 5, 80),
    ('Financial Analyst', 'Finance', 'POWERBI_FINANCE', 5, 5, 100),
    ('Financial Analyst', 'Finance', 'FIN_SHAREPOINT', 1, 5, 20),
    ('Software Engineer', 'Technology', 'JIRA_USER', 3, 3, 100),
    ('Software Engineer', 'Technology', 'GITHUB_DEV', 3, 3, 100),
    ('Software Engineer', 'Technology', 'CONFLUENCE_USER', 2, 3, 67),
    ('Risk Analyst', 'Risk', 'RSA_GRC', 1, 1, 100),
    ('Risk Analyst', 'Risk', 'POWERBI_RISK', 1, 1, 100),
    ('Risk Analyst', 'Risk', 'RISK_PORTAL', 1, 1, 100),
    ('Internal Auditor', 'Audit', 'AUDIT_TOOL', 1, 1, 100),
    ('Internal Auditor', 'Audit', 'POWERBI_AUDIT', 1, 1, 100),
    ('Internal Auditor', 'Audit', 'SHAREPOINT_AUDIT', 1, 1, 100),
    ('HR Specialist', 'HR', 'WORKDAY_HR', 1, 1, 100),
    ('Cloud Engineer', 'Technology', 'AWS_CONSOLE', 1, 1, 100),
    ('Cloud Engineer', 'Technology', 'JIRA_USER', 1, 1, 100),
    ('Cloud Engineer', 'Technology', 'GITHUB_DEV', 1, 1, 100),
    ('Senior Financial Analyst', 'Finance', 'SAP_FIN_DISPLAY', 1, 1, 100),
    ('Senior Financial Analyst', 'Finance', 'SAP_AP_INVOICE', 1, 1, 100),
    ('Senior Financial Analyst', 'Finance', 'POWERBI_FINANCE', 1, 1, 100);

INSERT INTO policy_rules (policy_id, policy_name, type, rule) VALUES
    ('POL001', 'Finance Birthright', 'ALLOW', 'Financial Analyst -> SAP_FIN_DISPLAY'),
    ('POL002', 'Finance Birthright', 'ALLOW', 'Financial Analyst -> POWERBI_FINANCE'),
    ('POL003', 'Engineering Birthright', 'ALLOW', 'Software Engineer -> JIRA_USER'),
    ('POL004', 'Engineering Birthright', 'ALLOW', 'Software Engineer -> GITHUB_DEV'),
    ('POL005', 'Risk Review', 'HUMAN_APPROVAL', 'risk_score >= 70'),
    ('POL006', 'Critical Access', 'HUMAN_APPROVAL', 'risk_score >= 90'),
    ('POL007', 'Affinity Threshold', 'ALLOW', 'affinity_score >= 70'),
    ('POL008', 'HR Birthright', 'ALLOW', 'HR Specialist -> WORKDAY_HR'),
    ('POL009', 'Cloud Engineering Birthright', 'ALLOW', 'Cloud Engineer -> AWS_CONSOLE'),
    ('POL010', 'Cloud Engineering Birthright', 'ALLOW', 'Cloud Engineer -> GITHUB_DEV'),
    ('POL011', 'Senior Finance Birthright', 'ALLOW', 'Senior Financial Analyst -> SAP_FIN_DISPLAY'),
    ('POL012', 'Senior Finance Birthright', 'ALLOW', 'Senior Financial Analyst -> POWERBI_FINANCE'),
    ('POL013', 'Risk Birthright', 'ALLOW', 'Risk Analyst -> RISK_PORTAL'),
    ('POL014', 'Audit Birthright', 'ALLOW', 'Internal Auditor -> AUDIT_TOOL');

INSERT INTO sod_rules (sod_id, entitlement_1, entitlement_2, severity) VALUES
    ('SOD001', 'SAP_VENDOR_CREATE', 'SAP_PAYMENT_APPROVER', 'Critical'),
    ('SOD002', 'AD_DOMAIN_ADMIN', 'RSA_GRC', 'High'),
    ('SOD003', 'SAP_AP_INVOICE', 'SAP_VENDOR_CREATE', 'High');

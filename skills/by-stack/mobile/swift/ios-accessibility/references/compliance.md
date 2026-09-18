# Enterprise Compliance — Legal, VPAT & Documentation

Legal landscape, compliance frameworks, VPAT documentation, and Apple App Store expectations.

## Legal Landscape

### United States

**DOJ ADA Rule (April 2024):** State and local government mobile apps must conform to **WCAG 2.2 AA**.
- Deadline for populations >=50K: **April 2026**
- Deadline for smaller entities: **April 2027**

**ADA Title III:** Courts use WCAG 2.1/2.2 AA as the benchmark for private sector. **H1 2025: 2,014 lawsuits (37% increase YoY).** Settlements range from $5K to $6M+.

**Section 508:** Federal agencies and contractors must meet WCAG 2.0 AA (update to 2.2 expected).

### European Union

**EN 301 549 / European Accessibility Act (EAA):** Enforced since **June 2025** for EU private businesses. Equivalent to WCAG 2.1 AA. Covers mobile apps, websites, ATMs, ticketing machines, and e-books.

## VPAT (Voluntary Product Accessibility Template)

**Current version:** VPAT 2.5 (November 2023), INT edition covers all major standards.

### Structure
A completed VPAT produces an **Accessibility Conformance Report (ACR)** with:

| Column | Content |
|---|---|
| Criteria | WCAG success criterion number and name |
| Conformance Level | Supports / Partially Supports / Does Not Support / Not Applicable |
| Remarks and Explanations | Specific details about how the criterion is met or not met |

### Conformance Levels
- **Supports** — Fully meets the criterion
- **Partially Supports** — Some functionality meets, some doesn't
- **Does Not Support** — Criterion is not met
- **Not Applicable** — Criterion doesn't apply to this product

### Update Frequency
- After every significant product update
- Annually at minimum
- Required for US federal procurement, enterprise sales, EAA compliance

## Documentation Package for Regulated Industries

For enterprise sales, government contracts, and compliance audits, prepare:

1. **ACR (Accessibility Conformance Report)** — completed VPAT
2. **Accessibility Statement** — public commitment to accessibility, published on website/app
3. **Testing Methodology** — description of automated + manual testing approach
4. **Remediation Roadmap** — known issues with fix timelines and priorities
5. **Training Records** — evidence of developer accessibility training
6. **Audit History** — dated accessibility assessments with scope and findings
7. **Feedback Mechanism** — accessibility-specific bug report process (email, form, or in-app)

## Apple App Store Expectations

No explicit WCAG mandate, but Apple reviewers increasingly flag:
- Text readability (contrast, font size)
- Color contrast issues
- VoiceOver support gaps
- Dynamic Type support
- Dark Mode failures

**Apple Human Interface Guidelines expect:**
- 44x44pt minimum touch targets
- Dynamic Type support for all text
- Dark Mode support
- Reduce Motion and Reduce Transparency support
- Semantic system colors
- Full Keyboard Access compatibility

## Compliance Mapping by Industry

### Healthcare (HIPAA)
- All PHI displays must be accessible to screen readers
- Biometric auth must have accessible alternatives
- Error messages with PHI must not be announced publicly (use `.low` priority)
- Consent forms must be navigable by VoiceOver

### Financial (PCI DSS)
- Payment forms must be accessible and properly labeled
- Session timeouts must warn with accessible notifications
- Multi-factor auth must support assistive technology
- Transaction confirmations must be announced

### Government (Section 508 / ADA)
- WCAG 2.0 AA minimum (2.2 AA expected)
- VPAT/ACR required for procurement
- Regular third-party audits
- Public accessibility statement required

### EU (EAA / EN 301 549)
- WCAG 2.1 AA minimum
- Applies to all digital products sold in EU since June 2025
- Includes mobile apps, not just websites
- Requires accessibility statement and feedback mechanism

# Gold-set review sheet

Twenty assistant-curated draft labels from real stored filings. Human review is pending. No extraction model or GEPA run produced these labels. Source excerpts are contiguous and preserve the existing masks; source IDs and URLs below are local review material, not model input.

| ID | Split | Issuer | Case | Entities | Relationships |
|---|---|---|---|---:|---:|
| gold_01 | train | NVDA | Two named suppliers, different inputs | 8 | 11 |
| gold_02 | train | NVDA | Suppliers listed as competitors | 6 | 8 |
| gold_03 | holdout | TSM | Customer list without a supplied item | 5 | 9 |
| gold_04 | train | NVDA | Customer A is not Company A | 0 | 0 |
| gold_05 | train | MU | Collaboration quote does not prove supply | 5 | 6 |
| gold_06 | holdout | TSM | Quoted customer and filing issuer differ | 5 | 7 |
| gold_07 | train | AMD | Foundry output is not the downstream processor | 5 | 7 |
| gold_08 | train | MU | Arbitration, ended joint venture, and three dates | 6 | 12 |
| gold_09 | validation | INTC | Signed acquisition is not a completed acquisition | 2 | 3 |
| gold_10 | validation | INTC | Equity purchase is not commercial supply | 4 | 8 |
| gold_11 | validation | AVGO | Acquirer does not inherit every target assertion | 3 | 4 |
| gold_12 | train | AMAT | Debt issued, repayment merely intended | 2 | 3 |
| gold_13 | train | KLAC | Maintenance revenue is a service | 2 | 3 |
| gold_14 | holdout | ASML | Shipped equipment with unnamed customers | 7 | 9 |
| gold_15 | validation | INTC | EPS down versus last year is not an earnings miss | 1 | 1 |
| gold_16 | holdout | ASML | Table of contents is navigation, not facts | 0 | 0 |
| gold_17 | train | AMAT | Unbound exhibit speaker | 0 | 0 |
| gold_18 | train | LRCX | Cybersecurity risk is not an actual breach | 2 | 2 |
| gold_19 | train | MU | Sector, product families and theme in one paragraph | 6 | 10 |
| gold_20 | train | KLAC | Software license versus commercial support | 2 | 3 |

## gold_01: Two named suppliers, different inputs

**train · NVDA · 10-K / 10-K · filed 2026-02-25**

[Source filing](https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm) · `NVDA/0001045810-26-000021/nvda-20260125.htm#section-0002-chunk-0004`

Masked chunk character interval: [4389, 5178).

**Input excerpt**

```text
We have expanded our supplier relationships to build redundancy and resilience in our operations to provide long-term manufacturing capacity aligned with growing customer demand. While currently our supply chain is mainly concentrated in Asia, we are expanding into the U.S. and Latin America. We utilize foundries, such as Company J Company Limited, or Company J, and Samsung Electronics Co., Ltd., or Samsung, to produce our semiconductor wafers. We purchase memory from SK Hynix Inc., Company I, Inc., and Samsung. We utilize CoWoS technology for semiconductor packaging. We engage with independent subcontractors and contract manufacturers such as Hon Hai Precision Industry Co., Ltd., Wistron Corporation, and Fabrinet to perform assembly, testing and packaging of our final products.
```

**Expected entities**

- Company: Company A
- Company: Company J
- Company: Company I
- Product: semiconductor wafers
- Product: memory
- Service: produce our semiconductor wafers
- Service: semiconductor packaging
- Service: assembly, testing and packaging

**Expected relationships**

- Company J → `supplies_to` → Company A (supplied_product=semiconductor wafers; supplied_service=produce our semiconductor wafers)
- Company I → `supplies_to` → Company A (supplied_product=memory)
- Company J → `provides` → produce our semiconductor wafers
- SourceDocument → `mentions` → Company A
- SourceDocument → `mentions` → Company J
- SourceDocument → `mentions` → Company I
- SourceDocument → `mentions` → semiconductor wafers
- SourceDocument → `mentions` → memory
- SourceDocument → `mentions` → produce our semiconductor wafers
- SourceDocument → `mentions` → semiconductor packaging
- SourceDocument → `mentions` → assembly, testing and packaging

**Reject these inferences**

- Company A supplies_to Company I/J
- Attach memory to the Company J supply edge
- Attach semiconductor wafers to Company I
- Add Samsung, SK Hynix or assembly subcontractors as Company nodes
- Company A provides the services it purchases

**Adjudication:** Physical wafers and memory are the supplied inputs. Foundry production also explicitly describes manufacturing work; packaging use alone does not identify a basket provider. Do not spread a list of inputs across a list of suppliers.

## gold_02: Suppliers listed as competitors

**train · NVDA · 10-K / 10-K · filed 2026-02-25**

[Source filing](https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm) · `NVDA/0001045810-26-000021/nvda-20260125.htm#section-0002-chunk-0005`

Masked chunk character interval: [1075, 1406).

**Input excerpt**

```text
Our current competitors include:

• suppliers and licensors of hardware and software for discrete and integrated GPUs, custom chips and other accelerated computing solutions, including solutions offered for AI, such as Company C, Inc., or Company C, Huawei Technologies Co. Ltd., or Huawei, and Company F Corporation, or Company F;
```

**Expected entities**

- Company: Company A
- Company: Company C
- Company: Company F
- Product: discrete and integrated GPUs
- Product: custom chips
- Topic: AI

**Expected relationships**

- Company A → `competes_with` → Company C
- Company A → `competes_with` → Company F
- SourceDocument → `has_topic` → AI
- SourceDocument → `mentions` → Company A
- SourceDocument → `mentions` → Company C
- SourceDocument → `mentions` → Company F
- SourceDocument → `mentions` → discrete and integrated GPUs
- SourceDocument → `mentions` → custom chips

**Reject these inferences**

- Company C/F supplies_to Company A
- Company C competes_with Company F
- Give every listed product to every listed competitor
- Add Huawei as a Company
- Represent software licenses as physical Products

**Adjudication:** The list establishes two competitor edges against the issuer. Listed physical product categories are mentioned but the list does not unambiguously allocate each category to each competitor. AI is a document topic; broader entity-level associations are permissible only with supporting evidence.

## gold_03: Customer list without a supplied item

**holdout · TSM · 20-F / 20-F · filed 2025-04-17**

[Source filing](https://www.sec.gov/Archives/edgar/data/1046179/000119312525083423/d896993d20f.htm) · `TSM/0001193125-25-083423/d896993d20f.htm#section-0005-chunk-0000`

Masked chunk character interval: [2160, 2610).

**Input excerpt**

```text
We count among our customers many of the world’s leading semiconductor companies, ranging from fabless semiconductor companies, system companies to integrated device manufacturers, including, but not limited to, Company C, Inc., Amazon Web Services, Inc., Company E Inc., Company F Corporation, MediaTek Inc., Company A Corporation, NXP Semiconductors N.V., Qualcomm Inc., Renesas Electronics Corporation and Sony Semiconductor Solutions Corporation.
```

**Expected entities**

- Company: Company J
- Company: Company C
- Company: Company E
- Company: Company F
- Company: Company A

**Expected relationships**

- Company J → `supplies_to` → Company C
- Company J → `supplies_to` → Company E
- Company J → `supplies_to` → Company F
- Company J → `supplies_to` → Company A
- SourceDocument → `mentions` → Company J
- SourceDocument → `mentions` → Company C
- SourceDocument → `mentions` → Company E
- SourceDocument → `mentions` → Company F
- SourceDocument → `mentions` → Company A

**Reject these inferences**

- Fill supplied_product or supplied_service from prior knowledge
- Reverse any customer edge
- Competition among listed customers
- Add AWS, Qualcomm or other outside companies
- Assign the customers' industry to the issuer solely from this sentence

**Adjudication:** Use the four direct Company J supply edges with empty supplied-item arrays. semiconductor companies describes customer classification; it does not name a separate market category to assign mechanically to every customer.

## gold_04: Customer A is not Company A

**train · NVDA · 10-Q / 10-Q · filed 2025-11-19**

[Source filing](https://www.sec.gov/Archives/edgar/data/1045810/000104581025000230/nvda-20251026.htm) · `NVDA/0001045810-25-000230/nvda-20251026.htm#section-0002-chunk-0009`

Masked chunk character interval: [1050, 1619).

**Input excerpt**

```text
For the third quarter of fiscal year 2026, four direct customers with sales greater than 10% of total revenue included: Customer A at 22%, Customer B at 15%, Customer C at 13%, and Customer D at 11%, which were attributable to the Compute & Networking segment. For the first nine months of fiscal year 2026, sales to two direct customers represented 21% and 13% of total revenue, respectively, both of which were attributable to the Compute & Networking segment. The customers referenced above may represent different customers than those reported in a previous period.
```

**Expected entities**

None.

**Expected relationships**

None.

**Reject these inferences**

- Create any Company from Customer A/B/C/D
- Infer a named customer from its revenue share
- Invent a customer concentration Topic whose name is absent
- Turn percentage disclosures into an earnings event

**Adjudication:** Empty result. No masked company is named or unambiguously referenced by an issuer pronoun. Compute & Networking is an internal segment label, not a separate Company, physical Product, Service, or industry Sector here.

## gold_05: Collaboration quote does not prove supply

**train · MU · 8-K / EX-99.1 · filed 2025-06-12**

[Source filing](https://www.sec.gov/Archives/edgar/data/723125/000110465925058741/tm2517778d1_ex99-1.htm) · `MU/0001104659-25-058741/tm2517778d1_ex99-1.htm#section-0003-chunk-0000`

Masked chunk character interval: [0, 494).

**Input excerpt**

```text
Jensen Huang, Founder and CEO, Company A

"Company I’s investment in advanced memory manufacturing and HBM capabilities in the U.S., with support from Trump Administration, is an important step forward for the AI ecosystem. Company I’s leadership in high-performance memory is invaluable to enabling the next generation of AI breakthroughs that Company A is driving. We’re excited to collaborate with Company I as we push the boundaries of what’s possible in AI and high-performance computing."
```

**Expected entities**

- Company: Company A
- Company: Company I
- Product: high-performance memory
- Topic: AI
- Topic: high-performance computing

**Expected relationships**

- Company A → `has_topic` → AI
- Company I → `has_topic` → AI
- Company A → `has_topic` → high-performance computing
- SourceDocument → `mentions` → Company A
- SourceDocument → `mentions` → Company I
- SourceDocument → `mentions` → high-performance memory

**Reject these inferences**

- Company I supplies_to Company A
- Resolve quoted we to Company I
- Use filing_date as the investment event date
- Add a collaborates_with edge outside the ontology
- Assume a named commercial service from in-house manufacturing investment

**Adjudication:** Company A is the explicitly attributed speaker. high-performance memory is a physical family mentioned in the quote; leadership alone is insufficient for a separate offers edge. AI and high-performance computing are literal topical associations, not measured EPS exposure.

## gold_06: Quoted customer and filing issuer differ

**holdout · TSM · 6-K / 6-K · filed 2024-04-08**

[Source filing](https://www.sec.gov/Archives/edgar/data/1046179/000104617924000040/a20240408.htm) · `TSM/0001046179-24-000040/a20240408.htm#section-0013-chunk-0000`

Masked chunk character interval: [29, 615).

**Input excerpt**

```text
“Today’s announcement highlights the strong commitment from Secretary Raimondo and the entire administration to ensure the U.S. plays a central role creating a more geographically diverse and resilient semiconductor supply chain,” said Company C Chair and CEO Lisa Su. “Company J has a long track record of providing the leading-edge manufacturing capabilities that have enabled Company C to focus on what we do best, designing high-performance chips that change the world. We are committed to our partnership with Company J and look forward to building our most advanced chips in U.S.”
```

**Expected entities**

- Company: Company J
- Company: Company C
- Service: leading-edge manufacturing capabilities
- Product: high-performance chips
- Topic: semiconductor supply chain

**Expected relationships**

- Company J → `supplies_to` → Company C (supplied_service=leading-edge manufacturing capabilities; time=a long track record)
- Company J → `provides` → leading-edge manufacturing capabilities (time=a long track record)
- SourceDocument → `has_topic` → semiconductor supply chain
- SourceDocument → `mentions` → Company J
- SourceDocument → `mentions` → Company C
- SourceDocument → `mentions` → leading-edge manufacturing capabilities
- SourceDocument → `mentions` → high-performance chips

**Reject these inferences**

- Resolve the quoted we to Company J
- Company C supplies_to Company J
- Assert U.S. manufacturing has already begun
- Invent an announcement Event from the vague Today's announcement reference

**Adjudication:** leading-edge manufacturing capabilities denotes customer manufacturing work, a Service. high-performance chips is a mentioned Product; the quote does not establish that chips have already been delivered from the future U.S. site. A long track record is retained as a literal historical qualifier, not converted to a date.

## gold_07: Foundry output is not the downstream processor

**train · AMD · 10-K / 10-K · filed 2026-02-04**

[Source filing](https://www.sec.gov/Archives/edgar/data/2488/000000248826000018/amd-20251227.htm) · `AMD/0000002488-26-000018/amd-20251227.htm#section-0003-chunk-0005`

Masked chunk character interval: [2191, 2588).

**Input excerpt**

```text
We utilize third-party wafer foundries to fabricate the silicon wafers for all of our products. We rely on Company J Company Limited (Company J) for the production of all wafers for microprocessor and GPU products at 7 nanometer (nm) or smaller nodes, and we rely primarily on GLOBALFOUNDRIES Inc. (GF) for wafers for microprocessor and GPU products manufactured at process nodes larger than 7 nm.
```

**Expected entities**

- Company: Company C
- Company: Company J
- Product: silicon wafers
- Product: microprocessor and GPU products
- Service: production of all wafers

**Expected relationships**

- Company J → `supplies_to` → Company C (supplied_product=silicon wafers; supplied_service=production of all wafers)
- Company J → `provides` → production of all wafers
- SourceDocument → `mentions` → Company C
- SourceDocument → `mentions` → Company J
- SourceDocument → `mentions` → silicon wafers
- SourceDocument → `mentions` → microprocessor and GPU products
- SourceDocument → `mentions` → production of all wafers

**Reject these inferences**

- Attach microprocessor and GPU products as the supplied item on J->C
- Company C provides its purchased foundry service
- Add GLOBALFOUNDRIES as a basket Company
- Infer Company J produces every node size

**Adjudication:** silicon wafers is the material output. production of all wafers is the literal commercial manufacturing work. microprocessor and GPU products are the customer's downstream product family; their mention does not establish a separate commercial offering here. Preserve the 7 nm scope in the evidence.

## gold_08: Arbitration, ended joint venture, and three dates

**train · MU · 10-Q / 10-Q · filed 2022-07-01**

[Source filing](https://www.sec.gov/Archives/edgar/data/723125/000072312522000036/mu-20220602.htm) · `MU/0000723125-22-000036/mu-20220602.htm#section-0002-chunk-0007`

Masked chunk character interval: [2331, 2827).

**Input excerpt**

```text
On July 31, 2020, Company I and Company F entered into a binding arbitration agreement under which the parties agreed to present to an arbitral panel various financial disputes related to the former IMFT joint venture between Company I and Company F, which ended October 31, 2019, and to other agreements related to the joint development, production, and sale of non-volatile memory products. On April 15, 2022, the arbitration panel issued a final order and concluded the arbitration proceeding.
```

**Expected entities**

- Company: Company I
- Company: Company F
- Product: non-volatile memory products
- Event: Binding arbitration agreement — arbitration_agreement, July 31, 2020
- Event: IMFT joint venture ended — joint_venture_ended, October 31, 2019
- Event: Arbitration final order — arbitration_concluded, April 15, 2022

**Expected relationships**

- Binding arbitration agreement → `concerns` → Company I (time=July 31, 2020)
- Binding arbitration agreement → `concerns` → Company F (time=July 31, 2020)
- IMFT joint venture ended → `concerns` → Company I (time=October 31, 2019)
- IMFT joint venture ended → `concerns` → Company F (time=October 31, 2019)
- Arbitration final order → `concerns` → Company I (time=April 15, 2022)
- Arbitration final order → `concerns` → Company F (time=April 15, 2022)
- SourceDocument → `mentions` → Company I
- SourceDocument → `mentions` → Company F
- SourceDocument → `mentions` → non-volatile memory products
- SourceDocument → `reports` → Binding arbitration agreement (time=July 31, 2020)
- SourceDocument → `reports` → IMFT joint venture ended (time=October 31, 2019)
- SourceDocument → `reports` → Arbitration final order (time=April 15, 2022)

**Reject these inferences**

- Any Company I/F supplies_to edge
- Any competes_with edge from the dispute
- Use the filing date for any of the three events
- Add owns or joint_venture edges outside the ontology

**Adjudication:** The agreement, joint-venture ending, and final arbitration order are distinct Events concerning both named companies. non-volatile memory products is mentioned only; no commercial supplier is specified.

## gold_09: Signed acquisition is not a completed acquisition

**validation · INTC · 10-Q / 10-Q · filed 2026-10-30**

[Source filing](https://www.sec.gov/Archives/edgar/data/50863/000005086326000191/intc-20260926.htm) · `INTC/0000050863-26-000191/intc-20260926.htm#section-0008-chunk-0008`

Masked chunk character interval: [0, 625).

**Input excerpt**

```text
In October 2026, we entered into a definitive agreement to acquire 100% of Classiq Technologies Ltd. for approximately $1.9 billion, subject to customary purchase price adjustments. The agreement also provides for up to $350 million of contingent consideration based on the achievement of specified performance milestones. The transaction is expected to close in the first half of 2027, subject to regulatory approvals and other customary closing conditions. We may elect to settle the purchase price in cash or shares of Company F common stock. We have not yet completed our assessment of the accounting for the transaction.
```

**Expected entities**

- Company: Company F
- Event: Classiq acquisition agreement — acquisition_agreement, October 2026

**Expected relationships**

- Classiq acquisition agreement → `concerns` → Company F (time=October 2026)
- SourceDocument → `mentions` → Company F
- SourceDocument → `reports` → Classiq acquisition agreement (time=October 2026)

**Reject these inferences**

- A completed acquisition Event
- An event dated first half of 2027 as if it occurred
- A Classiq Company node
- A common stock Product or a supply edge

**Adjudication:** Extract only the acquisition agreement occurrence with October 2026. A future expected closing date does not replace that date; the outside-basket target can appear in the event description without becoming a Company node.

## gold_10: Equity purchase is not commercial supply

**validation · INTC · 10-Q / 10-Q · filed 2026-10-30**

[Source filing](https://www.sec.gov/Archives/edgar/data/50863/000005086326000191/intc-20260926.htm) · `INTC/0000050863-26-000191/intc-20260926.htm#section-0008-chunk-0004`

Masked chunk character interval: [618, 929).

**Input excerpt**

```text
On September 15, 2025, we entered into an agreement to issue and sell 215 million shares of our common stock to Company A at a price of $23.28 per share, representing an aggregate cash purchase price of $5.0 billion. The issuance and sale of the shares were completed the following quarter on December 26, 2025.
```

**Expected entities**

- Company: Company F
- Company: Company A
- Event: Equity sale agreement — equity_sale_agreement, September 15, 2025
- Event: Equity sale completed — equity_sale_completed, December 26, 2025

**Expected relationships**

- Equity sale agreement → `concerns` → Company F (time=September 15, 2025)
- Equity sale agreement → `concerns` → Company A (time=September 15, 2025)
- Equity sale completed → `concerns` → Company F (time=December 26, 2025)
- Equity sale completed → `concerns` → Company A (time=December 26, 2025)
- SourceDocument → `mentions` → Company F
- SourceDocument → `mentions` → Company A
- SourceDocument → `reports` → Equity sale agreement (time=September 15, 2025)
- SourceDocument → `reports` → Equity sale completed (time=December 26, 2025)

**Reject these inferences**

- Company F supplies_to Company A
- Company A supplies_to Company F
- A shares or common stock Product
- Merge September 15 and December 26 into one event date

**Adjudication:** The second sentence is resolved to the shares and counterparties in the first. Both events concern both companies; selling shares is outside supplies_to in this company operating network.

## gold_11: Acquirer does not inherit every target assertion

**validation · AVGO · 10-K / 10-K · filed 2025-12-18**

[Source filing](https://www.sec.gov/Archives/edgar/data/1730168/000173016825000121/avgo-20251102.htm) · `AVGO/0001730168-25-000121/avgo-20251102.htm#section-0014-chunk-0004`

Masked chunk character interval: [1799, 2501).

**Input excerpt**

```text
On November 22, 2023, we completed the acquisition of VMware, Inc. (“VMware”) in a cash-and-stock transaction (the “VMware Merger”). The VMware stockholders received approximately $30,788 million in cash and 544 million shares of Company E common stock (on a split adjusted basis) with a fair value of $53,398 million. VMware was a leading provider of multi-cloud services for all applications, enabling digital innovation with enterprise control. We acquired VMware to enhance our infrastructure software capabilities. The accompanying consolidated financial statements include the results of operations of VMware commencing on November 22, 2023. See Note 4. “Acquisitions” for additional information.
```

**Expected entities**

- Company: Company E
- Service: multi-cloud services
- Event: VMware acquisition completed — acquisition_completed, November 22, 2023

**Expected relationships**

- VMware acquisition completed → `concerns` → Company E (time=November 22, 2023)
- SourceDocument → `mentions` → Company E
- SourceDocument → `mentions` → multi-cloud services
- SourceDocument → `reports` → VMware acquisition completed (time=November 22, 2023)

**Reject these inferences**

- Add VMware as a basket Company
- Company E provides multi-cloud services solely from VMware's description
- Any acquisition/owns edge outside the ontology
- Classify shares or software as physical Products

**Adjudication:** The VMware acquisition is an actual Event concerning Company E. multi-cloud services is a Service mention. Preserve its original attribution instead of transferring provides to the acquirer. infrastructure software capabilities is not an industry classification or a physical Product.

## gold_12: Debt issued, repayment merely intended

**train · AMAT · 8-K / 8-K · filed 2025-09-19**

[Source filing](https://www.sec.gov/Archives/edgar/data/6951/000119312525209109/d82455d8k.htm) · `AMAT/0001193125-25-209109/d82455d8k.htm#section-0001-chunk-0000`

Masked chunk character interval: [55, 1354).

**Input excerpt**

```text
On September 18, 2025, Company B, Inc. (“Applied”) completed a registered public offering of $550 million in aggregate principal amount of 4.000% senior unsecured notes due 2031 (the “2031 Notes”) and $450 million in aggregate principal amount of 4.600% senior unsecured notes due 2036 (the “2036 Notes” and, collectively with the 2031 Notes, the “Notes”) pursuant to an underwriting agreement (the “Underwriting Agreement”) with Citigroup Global Markets Inc., Mizuho Securities USA LLC and MUFG Securities Americas Inc., as representatives of the several underwriters named therein, and an indenture dated as of June 11, 2024 (the “Base Indenture”), between Applied and The Bank of New York Mellon Trust Company, N.A., as trustee, as supplemented by a supplemental indenture dated as of September 18, 2025 (the “Second Supplemental Indenture” and, together with the Base Indenture, the “Indenture”). Applied intends to use a portion of the net proceeds from the offering to repay at maturity its outstanding $700 million in aggregate principal amount of 3.900% senior unsecured notes due October 1, 2025 and the remaining net proceeds from the offering for general corporate purposes. The Notes were issued and sold pursuant to Applied’s registration statement on Form S-3ASR (File No. 333-279682).
```

**Expected entities**

- Company: Company B
- Event: Senior unsecured notes issued — debt_issuance_completed, September 18, 2025

**Expected relationships**

- Senior unsecured notes issued → `concerns` → Company B (time=September 18, 2025)
- SourceDocument → `mentions` → Company B
- SourceDocument → `reports` → Senior unsecured notes issued (time=September 18, 2025)

**Reject these inferences**

- Completed debt repayment on October 1, 2025
- Products for Notes, common stock, or underwriting agreements
- Company nodes for underwriters or trustee
- supplies_to from a securities sale

**Adjudication:** Extract the completed debt issuance. June 11, 2024 dates a referenced indenture, not a separately described occurrence. The supplemental indenture and registration references are supporting legal-document context, not separate extracted SourceDocuments.

## gold_13: Maintenance revenue is a service

**train · KLAC · 10-K / 10-K · filed 2026-08-06**

[Source filing](https://www.sec.gov/Archives/edgar/data/319201/000031920126000027/klac-20260630.htm) · `KLAC/0000319201-26-000027/klac-20260630.htm#section-0002-chunk-0000`

Masked chunk character interval: [951, 1155).

**Input excerpt**

```text
Our services business, which accounted for approximately 23% of our total revenues in fiscal 2026, provides maintenance and other services to maximize uptime, productivity and tool life for our customers.
```

**Expected entities**

- Company: Company G
- Service: maintenance

**Expected relationships**

- Company G → `provides` → maintenance
- SourceDocument → `mentions` → Company G
- SourceDocument → `mentions` → maintenance

**Reject these inferences**

- maintenance as a Product
- Invent a named customer or supplies_to edge
- Turn the revenue percentage into an earnings miss

**Adjudication:** maintenance is the specific named Service. other services is too generic to create another offering. The fiscal year qualifies the revenue percentage, not necessarily the full duration of service provision.

## gold_14: Shipped equipment with unnamed customers

**holdout · ASML · 20-F / 20-F · filed 2026-02-25**

[Source filing](https://www.sec.gov/Archives/edgar/data/937966/000162828026011378/asml-20251231.htm) · `ASML/0001628280-26-011378/asml-20251231.htm#section-0004-chunk-0000`

Masked chunk character interval: [3151, 3521).

**Input excerpt**

```text
Latest: TWINSCAN NXE:3800E reaches full productivity specification In 2025, we shipped TWINSCAN NXE:3800E systems to our customers at full specification, which includes 220 wafers-per-hour throughput – a 37% improvement compared to the TWINSCAN NXE:3600D – a higher-power light source, new wafer handler, faster wafer stages and high-power imaging control functionality.
```

**Expected entities**

- Company: Company D
- Product: TWINSCAN NXE:3800E
- Product: TWINSCAN NXE:3600D
- Product: higher-power light source
- Product: wafer handler
- Product: wafer stages
- Event: TWINSCAN NXE:3800E systems shipped — product_shipments, In 2025

**Expected relationships**

- Company D → `offers` → TWINSCAN NXE:3800E (time=In 2025)
- TWINSCAN NXE:3800E systems shipped → `concerns` → Company D (time=In 2025)
- SourceDocument → `mentions` → Company D
- SourceDocument → `mentions` → TWINSCAN NXE:3800E
- SourceDocument → `mentions` → TWINSCAN NXE:3600D
- SourceDocument → `mentions` → higher-power light source
- SourceDocument → `mentions` → wafer handler
- SourceDocument → `mentions` → wafer stages
- SourceDocument → `reports` → TWINSCAN NXE:3800E systems shipped (time=In 2025)

**Reject these inferences**

- Invent a named supplies_to customer
- Assume the comparison model is still offered
- Treat throughput units as a supply quantity to a named company
- A provides edge for software functionality

**Adjudication:** Actual shipments to customers support a commercial offering and one shipment Event. The older comparison model and physical components are mentions only; being a component does not prove a separately offered product. Wafers-per-hour is a unit, not another offered Product. The headline describes the same model, not a second entity.

## gold_15: EPS down versus last year is not an earnings miss

**validation · INTC · 8-K / EX-99.1 · filed 2024-01-25**

[Source filing](https://www.sec.gov/Archives/edgar/data/50863/000005086324000008/q423_earningsrelease.htm) · `INTC/0000050863-24-000008/q423_earningsrelease.htm#section-0003-chunk-0000`

Masked chunk character interval: [2660, 3173).

**Input excerpt**

```text
	GAAP			Non-GAAP		
	2023	2022	vs. 2022	2023	2022	vs. 2022
Revenue ($B)	$54.2	$63.1	down 14%			
Gross margin	40.0%	42.6%	down 2.6 ppts	43.6%	47.3%	down 3.7 ppts
R&D and MG&A ($B)	$21.7	$24.5	down 12%	$19.0	$21.9	down 13%
Operating margin	0.2%	3.7%	down 3.5 ppts	8.6%	12.6%	down 4 ppts
Tax rate	(119.8)%	(3.2)%	down 116.6 ppts	13.0%	13.0%	—
Net income attributable to Company F ($B)	$1.7	$8.0	down 79%	$4.4	$6.9	down 36%
Earnings per share attributable to Company F—diluted	$0.40	$1.94	down 79%	$1.05	$1.67	down 37%
```

**Expected entities**

- Company: Company F

**Expected relationships**

- SourceDocument → `mentions` → Company F

**Reject these inferences**

- Any earnings_miss Event
- Invent a consensus value
- Merge GAAP and non-GAAP figures
- Treat 2023 or filing_date as an announcement date
- Product nodes for income, EPS or revenue

**Adjudication:** Only the explicit Company F mention is canonical. Under this dataset policy, a periodic accounting snapshot alone is not a discrete Event; an explicit announcement or specific occurrence is needed. A named company in the body of a financial table is a substantive mention, unlike a table of contents.

## gold_16: Table of contents is navigation, not facts

**holdout · ASML · 20-F / 20-F · filed 2026-02-25**

[Source filing](https://www.sec.gov/Archives/edgar/data/937966/000162828026011378/asml-20251231.htm) · `ASML/0001628280-26-011378/asml-20251231.htm#section-0000-chunk-0001`

Masked chunk character interval: [441, 1215).

**Input excerpt**

```text
1. Strategic report	
5	Special note regarding forward- looking statements
6	At a glance – 2025 overview
8	In conversation with our CEO
	Our business
14	Our holistic approach to lithography
18	Our products and services
22	Our marketplace
28	Our business strategy
29	Deepen customer trust
31	Extend our technology and holistic product leadership
33	Strengthen ecosystem relationships
35	Create an exceptional workplace
37	Drive operational excellence
39	Deliver on ESG sustainability
41	Our business model
44	Engaged stakeholders
	Financial performance
51	Message from our CFO
54	Financial performance KPIs
59	Long-term growth opportunities
	Risk and security
61	Understanding Company D’s risk management framework
63	How we manage risk
66	Risk factors
74	Information security
```

**Expected entities**

None.

**Expected relationships**

None.

**Reject these inferences**

- Company D entity solely from the navigational row
- has_topic from navigational section names
- Events from chapter titles

**Adjudication:** Empty result. The substantive-mention rule excludes this table of contents, even though text contains a company label and risk-related headings.

## gold_17: Unbound exhibit speaker

**train · AMAT · 8-K / EX-99.1 · filed 2026-02-12**

[Source filing](https://www.sec.gov/Archives/edgar/data/6951/000162828026007661/exhibit991q12026earningsre.htm) · `AMAT/0001628280-26-007661/exhibit991q12026earningsre.htm#section-0002-chunk-0000`

Masked chunk character interval: [1075, 1263).

**Input excerpt**

```text
“With demand increasing for our innovative products and services, we are focused on ensuring we have the capacity to support our customers,” said Brice Hill, Senior Vice President and CFO.
```

**Expected entities**

None.

**Expected relationships**

None.

**Reject these inferences**

- Resolve we to Company B using filing_issuer metadata alone
- Use knowledge of Brice Hill's employer
- Generic products or services as named offerings
- A current supplies_to edge to unnamed customers

**Adjudication:** Empty under the current exhibit attribution rule. The full parent chunk may identify the employer; that text is deliberately outside this example and cannot be borrowed. This tests excerpt-local evidence, not the complete document.

## gold_18: Cybersecurity risk is not an actual breach

**train · LRCX · 10-K / 10-K · filed 2025-08-11**

[Source filing](https://www.sec.gov/Archives/edgar/data/707549/000070754925000075/lrcx-20250629.htm) · `LRCX/0000707549-25-000075/lrcx-20250629.htm#section-0003-chunk-0004`

Masked chunk character interval: [4498, 5735).

**Input excerpt**

```text
While we have implemented International Organization for Standardization (“ISO”) 27001 compliant security procedures and virus protection software, intrusion prevention systems, identity and access control, and emergency recovery processes, and we carefully select our third-party providers of information systems, to mitigate risks to the information systems that we rely on and to the technology, data, intellectual property and other sensitive information we seek to protect, those security procedures and mitigation and protection systems cannot be guaranteed to be fail-safe, and we may still suffer cybersecurity and other incidents, which could have a material adverse effect on our business or operations. It has been difficult and may continue to be difficult to hire and retain employees with substantial cybersecurity acumen. In addition, there have been and may continue to be instances of our policies and procedures not being effective in enabling us to identify risks, threats and incidents in a timely manner, or at all, or to respond expediently, appropriately and effectively when incidents occur and repair any damage caused by such incidents, and such occurrences could have a material adverse effect on our business.
```

**Expected entities**

- Company: Company H
- Topic: cybersecurity

**Expected relationships**

- Company H → `has_topic` → cybersecurity
- SourceDocument → `mentions` → Company H

**Reject these inferences**

- An actual cyberattack Event
- Company H provides cybersecurity services
- Company H offers its internally used software or systems
- Named supplier edges from third-party providers

**Adjudication:** cybersecurity is a literal relevant Topic linked to Company H. Internal controls are not commercial service offerings. The vague past failures have no identifiable occurrence time, so they do not create dated Events.

## gold_19: Sector, product families and theme in one paragraph

**train · MU · 10-K / 10-K · filed 2026-10-09**

[Source filing](https://www.sec.gov/Archives/edgar/data/723125/000072312526000023/mu-20260903.htm) · `MU/0000723125-26-000023/mu-20260903.htm#section-0002-chunk-0000`

Masked chunk character interval: [28, 472).

**Input excerpt**

```text
We are a global leader in semiconductor memory and storage, powering AI and compute-intensive applications from cloud to edge. With a relentless focus on our customers, technology and product leadership, and manufacturing and operational excellence, our comprehensive portfolio of high-performance DRAM, NAND, and NOR solutions deliver the speed, efficiency, and scale today’s workloads demand, accelerating intelligence to enrich life for all.
```

**Expected entities**

- Company: Company I
- Sector: semiconductor memory and storage
- Product: DRAM
- Product: NAND
- Product: NOR
- Topic: AI

**Expected relationships**

- Company I → `operates_in` → semiconductor memory and storage
- Company I → `offers` → DRAM
- Company I → `offers` → NAND
- Company I → `offers` → NOR
- Company I → `has_topic` → AI
- SourceDocument → `mentions` → Company I
- SourceDocument → `mentions` → semiconductor memory and storage
- SourceDocument → `mentions` → DRAM
- SourceDocument → `mentions` → NAND
- SourceDocument → `mentions` → NOR

**Reject these inferences**

- Named AI customer or supply edge from powering AI
- Quantified EPS exposure
- Classify AI as a physical Product
- Use a real company identity behind Company I

**Adjudication:** semiconductor memory and storage is the source-defined field of business. DRAM, NAND and NOR are physical memory families in the stated commercial portfolio. AI is a theme. These source phrases are canonical; source-supported product-family expansions may be accepted by an eventual adjudicated matcher.

## gold_20: Software license versus commercial support

**train · KLAC · 10-K / 10-K · filed 2026-08-06**

[Source filing](https://www.sec.gov/Archives/edgar/data/319201/000031920126000027/klac-20260630.htm) · `KLAC/0000319201-26-000027/klac-20260630.htm#section-0014-chunk-0007`

Masked chunk character interval: [3451, 4159).

**Input excerpt**

```text
We offer perpetual and term licenses for software products. The primary difference between perpetual and term licenses is the duration over which the customer can benefit from the use of the software, while the functionality and the features of the software are the same. Software is generally bundled with post-contract customer support (“PCS”), which includes unspecified software updates that are made available throughout the entire term of the arrangement. Revenue from software licenses is recognized at a point in time, when the software is made available to the customer. Revenue from PCS is deferred at contract inception and recognized ratably over the service period, or as services are performed.
```

**Expected entities**

- Company: Company G
- Service: post-contract customer support

**Expected relationships**

- Company G → `provides` → post-contract customer support
- SourceDocument → `mentions` → Company G
- SourceDocument → `mentions` → post-contract customer support

**Reject these inferences**

- A physical Product node for software, perpetual licenses or term licenses
- Treat license duration as a dated Event
- Invent a named customer
- Automatically classify every intangible license as a Service

**Adjudication:** The current physical-Product definition controls the gold. post-contract customer support is a commercial Service. Software updates are part of this named support bundle, not a separately asserted service offering. If Product is later widened to include software, this example must be relabelled with the ontology version.

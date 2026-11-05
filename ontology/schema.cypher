CREATE CONSTRAINT picton_Company_iri_unique IF NOT EXISTS FOR (n:Company) REQUIRE n.iri IS UNIQUE;

CREATE CONSTRAINT picton_Event_iri_unique IF NOT EXISTS FOR (n:Event) REQUIRE n.iri IS UNIQUE;

CREATE CONSTRAINT picton_Product_iri_unique IF NOT EXISTS FOR (n:Product) REQUIRE n.iri IS UNIQUE;

CREATE CONSTRAINT picton_Sector_iri_unique IF NOT EXISTS FOR (n:Sector) REQUIRE n.iri IS UNIQUE;

CREATE CONSTRAINT picton_Service_iri_unique IF NOT EXISTS FOR (n:Service) REQUIRE n.iri IS UNIQUE;

CREATE CONSTRAINT picton_SourceDocument_iri_unique IF NOT EXISTS FOR (n:SourceDocument) REQUIRE n.iri IS UNIQUE;

CREATE CONSTRAINT picton_Topic_iri_unique IF NOT EXISTS FOR (n:Topic) REQUIRE n.iri IS UNIQUE;

UNWIND ["buys_from", "competes_with", "concerns", "has_topic", "includes", "mentioned_in", "mentions", "offered_by", "offers", "operates_in", "provided_by", "provides", "reported_in", "reports", "subject_of", "supplies_to", "topic_of"] AS name CALL db.createRelationshipType(name);

UNWIND ["description", "event_type", "evidence_text", "exchange", "iri", "name", "occurred_at", "published_at", "supplied_product", "supplied_service", "supported_by", "ticker", "url"] AS name CALL db.createProperty(name);

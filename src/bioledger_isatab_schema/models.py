from pydantic import BaseModel


class OntologySourceSpec(BaseModel):
    name: str  # e.g. "OBI"
    file: str = ""  # e.g. "http://purl.obolibrary.org/obo/obi.owl"
    version: str = ""


class CharacteristicSpec(BaseModel):
    term: str
    ontology_ref: str = ""  # IRI


class SourceSpec(BaseModel):
    name: str
    characteristics: list[CharacteristicSpec] = []


class SampleSpec(BaseModel):
    name: str
    derived_from: str = ""  # source name
    characteristics: list[CharacteristicSpec] = []


class ISAStudySpec(BaseModel):
    """Structured output from the ISAForge agent."""

    investigation_id: str
    title: str
    description: str
    study_id: str
    study_title: str
    study_description: str
    ontology_sources: list[OntologySourceSpec] = []
    sources: list[SourceSpec] = []
    samples: list[SampleSpec] = []

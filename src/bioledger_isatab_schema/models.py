from pydantic import BaseModel, Field


class OntologySourceSpec(BaseModel):
    """A `Term Source` declared in `i_investigation.txt` (e.g. an ontology)."""

    name: str = Field(description='Ontology short name, e.g. "OBI".')
    file: str = Field(
        default="", description='Ontology URI, e.g. "http://purl.obolibrary.org/obo/obi.owl".'
    )
    version: str = Field(default="", description="Ontology version, if known.")


class CharacteristicSpec(BaseModel):
    """A single `Characteristics[...]` value attached to a source or sample."""

    term: str = Field(description="The characteristic's term/value, e.g. an organism name.")
    ontology_ref: str = Field(
        default="", description="IRI of the matching ontology term, if resolved."
    )


class SourceSpec(BaseModel):
    """A biological source material (`Source Name` column)."""

    name: str = Field(description="Source name, unique within the study.")
    characteristics: list[CharacteristicSpec] = Field(
        default_factory=list,
        description="Characteristics attached to this source (e.g. organism).",
    )


class SampleSpec(BaseModel):
    """A sample derived from a source (`Sample Name` column)."""

    name: str = Field(description="Sample name, unique within the study.")
    derived_from: str = Field(
        default="", description="Name of the source this sample was derived from."
    )
    characteristics: list[CharacteristicSpec] = Field(
        default_factory=list, description="Characteristics attached to this sample."
    )


class ISAStudySpec(BaseModel):
    """Structured output from the ISAForge agent."""

    investigation_id: str = Field(description="Investigation identifier.")
    title: str = Field(description="Investigation title.")
    description: str = Field(description="Investigation description.")
    study_id: str = Field(description="Study identifier.")
    study_title: str = Field(description="Study title.")
    study_description: str = Field(description="Study description.")
    ontology_sources: list[OntologySourceSpec] = Field(
        default_factory=list, description="Ontologies referenced by this study's terms."
    )
    sources: list[SourceSpec] = Field(
        default_factory=list, description="Biological source materials in this study."
    )
    samples: list[SampleSpec] = Field(
        default_factory=list, description="Samples derived from the sources."
    )

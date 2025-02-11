from Bio.SeqIO import SeqRecord
from structlog import BoundLogger, get_logger
from typing import Tuple


async def process_default(
    records: list, metadata: dict, filter_set: set, logger: BoundLogger = get_logger()
) -> Tuple[list, list]:
    """
    Format new sequences from NCBI Taxonomy if they do not already exist in the reference.

    :param records: A list of SeqRecords from NCBI Taxonomy
    :param metadata: A deserialized OTU metadata file
    :param filter_set: A set of accessions that should be omitted
    :param logger: Optional entry point for an existing BoundLogger
    :return: A list of processed new sequences/isolates and
        a set of automatically excluded accessions
    """
    auto_excluded = []
    otu_updates = []

    for seq_data in records:
        accession = seq_data.id.split(".")[0]
        seq_qualifier_data = get_qualifiers(seq_data.features)

        if accession in filter_set:
            logger.debug("Accession already exists", accession=seq_data.id)
            continue

        if check_source_type(seq_qualifier_data) is None:
            continue
        isolate = find_isolate_metadata(seq_qualifier_data)

        seq_dict = format_sequence(record=seq_data, qualifiers=seq_qualifier_data)

        if "segment" not in seq_dict:
            schema = metadata.get("schema", [])

            if schema:
                seq_dict["segment"] = schema[0].get("name", "")
            else:
                logger.warning('Missing schema')
                seq_dict["segment"] = ""

        seq_dict["isolate"] = isolate
        otu_updates.append(seq_dict)

    return otu_updates, auto_excluded


def format_sequence(record: SeqRecord, qualifiers: dict) -> dict:
    """
    Creates a new sequence file for a given isolate

    :param record: Genbank record object for a given accession
    :param qualifiers: Dictionary containing all qualifiers in the source field
        of the features section of a Genbank record
    :return: A dict containing the new sequence data and metadata
    """
    seq_dict = {
        "accession": record.id,
        "definition": record.description,
        "sequence": str(record.seq),
    }

    if (record_host := qualifiers.get('host', None)) is not None:
        seq_dict['host'] = record_host[0]

    if (record_segment := qualifiers.get('segment', None)) is not None:
        seq_dict['segment'] = record_segment[0]

    return seq_dict


def format_isolate(source_name: str, source_type: str, isolate_id: str) -> dict:
    """
    Formats raw isolate data for storage in a reference directory

    :param source_name: Assigned source name for an accession
    :param source_type: Assigned source type for an accession
    :param isolate_id: Unique ID number for this new isolate
    :return: A dict containing the new isolate.json contents
    """
    isolate = {
        "id": isolate_id,
        "source_type": source_type,
        "source_name": source_name,
        "default": False,
    }
    return isolate


def evaluate_sequence(
    seq_data,
    seq_qualifier_data,
    required_parts: dict,
    logger: BoundLogger = get_logger(),
):
    """
    Evaluates the validity of the record using catalogued metadata
    """

    record_segment_list = seq_qualifier_data.get("segment", None)
    logger.debug(f"segments={record_segment_list}")

    if len(required_parts) > 1:
        # This doesn't take into account segment name differences
        if record_segment_list is None:
            logger.debug("No segment name. Discarding record...")
            return False

        segment_name = record_segment_list[0]

        logger = logger.bind(record_segment=segment_name)
        if segment_name not in required_parts.keys():
            logger.debug("Required segment not found. Moving on...")
            return False

        if required_parts[segment_name] < 0:
            logger.error(
                f"Sequence length parameter for {segment_name} is not in catalog."
                + "Moving on..."
            )
            return False

        try:
            listed_length = required_parts[segment_name]
        except KeyError as e:
            logger.exception(e)
            listed_length = -1

    else:
        segment_name, listed_length = required_parts.copy().popitem()

    logger = logger.bind(segment=segment_name)

    if listed_length < 0:
        logger.warning("No valid length available for this segment.")

    seq_length = len(seq_data.seq)

    if not valid_length(seq_length, listed_length):
        logger.debug(
            "Bad length. Moving on...",
            seq_length=seq_length,
            listing_length=listed_length,
        )

        return False

    return True


def valid_length(seq_length: int, listed_length: int) -> bool:
    """
    Returns true if an integer is within 10% above or below a listed average value.
    Used to check if the length of a new sequence is within acceptable bounds.

    :param seq_length: Length of newly fetched sequence from GenBank
    :param listed_length: Average length of accepted sequences corresponding to this segment
    :return: Dictionary containing all qualifiers in the source field of the features section of a Genbank record
    """
    max_length = listed_length * 1.10
    min_length = listed_length * 0.9

    if seq_length > max_length or seq_length < min_length:
        return False

    return True


def get_qualifiers(seq: list) -> dict:
    """
    Get relevant qualifiers in a Genbank record

    :param seq: SeqIO features object for a particular accession
    :return: Dictionary containing all qualifiers in the source field of the features section of a Genbank record
    """
    qualifiers = {}

    for feature in [feature for feature in seq if feature.type == "source"]:
        for qual_key in feature.qualifiers:
            qualifiers[qual_key] = feature.qualifiers.get(qual_key)

    return qualifiers


def get_lengthdict_multipartite(
    schema: list, logger: BoundLogger = get_logger()
) -> dict:
    """
    Returns a dict of each required segment and its average length.

    :param schema: Augmented schema from the catalog listing, contains average length data
    :param logger: Optional entry point for an existing BoundLogger
    :return: A dict of required segments and their average lengths
    """
    required_part_lengths = {}

    for part in schema:
        if part["required"]:
            part_length = part.get("length", -1)
            if part_length < 0:
                logger.warning(f"{part['name']} lacks a length listing.")

            required_part_lengths[part["name"]] = part_length

    return required_part_lengths


def get_lengthdict_monopartite(
    schema: list, logger: BoundLogger = get_logger()
) -> dict:
    """
    Returns a dict of the segment name and its average length.
    Given that the OTU is monopartite, a filler segment name can be used if none is found.

    :param schema: Augmented schema from the catalog listing, contains average length data
    :param logger: Optional entry point for an existing BoundLogger
    :return: A dict of the single segment and its average length
    """
    part = schema[0]

    if (part_name := part.get("name", None)) is None:
        part_name = "unlabelled"

    part_length = part.get("length", -1)
    if part_length < 0:
        logger.warning(f"{part['name']} lacks a length listing.")

    return {part_name: part_length}


def find_isolate_metadata(qualifiers: dict) -> dict:
    isolate_type = check_source_type(qualifiers)
    isolate_name = qualifiers.get(isolate_type)[0]
    return {"source_name": isolate_name, "source_type": isolate_type}


def check_source_type(qualifiers: dict) -> str | None:
    """
    Determine the source type in a Genbank record

    :param qualifiers: Dictionary containing qualifiers in a features section of a Genbank record
    :return:
    """
    for qualifier in ["isolate", "strain"]:
        if qualifier in qualifiers:
            return qualifier

    return None

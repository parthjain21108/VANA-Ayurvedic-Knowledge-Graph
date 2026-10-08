import argparse
import pandas as pd


def main():
    parser = argparse.ArgumentParser(
        description="Generate HAS_PART relationships from Plant and PlantPart Excel files."
    )

    parser.add_argument(
        "--plant",
        required=True,
        help="Plant Excel file"
    )

    parser.add_argument(
        "--plantpart",
        required=True,
        help="PlantPart Excel file"
    )

    parser.add_argument(
        "--output",
        default="HAS_PART.xlsx",
        help="Output Excel file"
    )

    args = parser.parse_args()

    # ------------------------------------------------------------
    # READ FILES
    # ------------------------------------------------------------

    plant_df = pd.read_excel(args.plant)
    plantpart_df = pd.read_excel(args.plantpart)

    # Clean column names
    plant_df.columns = plant_df.columns.astype(str).str.strip()
    plantpart_df.columns = plantpart_df.columns.astype(str).str.strip()

    # ------------------------------------------------------------
    # VALIDATE COLUMNS
    # ------------------------------------------------------------

    if "scientific_name" not in plant_df.columns:
        raise ValueError(
            "Plant.xlsx must contain a 'scientific_name' column."
        )

    if "plant_name" not in plantpart_df.columns:
        raise ValueError(
            "PlantPart.xlsx must contain a 'plant_name' column."
        )

    if "plant_part_id" not in plantpart_df.columns:
        raise ValueError(
            "PlantPart.xlsx must contain a 'plant_part_id' column."
        )

    # ------------------------------------------------------------
    # GET PLANTS
    # ------------------------------------------------------------

    plants = set(
        plant_df["scientific_name"]
        .dropna()
        .astype(str)
        .str.strip()
    )

    # ------------------------------------------------------------
    # CREATE HAS_PART RELATIONSHIPS
    # ------------------------------------------------------------

    relationships = []

    for _, row in plantpart_df.iterrows():

        plant_name = str(row["plant_name"]).strip()
        plant_part_id = str(row["plant_part_id"]).strip()

        if not plant_name or plant_name.lower() == "nan":
            continue

        if not plant_part_id or plant_part_id.lower() == "nan":
            continue

        # Make sure this plant exists in Plant.xlsx
        if plant_name not in plants:
            print(
                f"WARNING: {plant_name} not found in Plant.xlsx "
                f"-> skipping"
            )
            continue

        relationships.append([
            "Plant",
            plant_name,
            "HAS_PART",
            "PlantPart",
            plant_part_id,
            "has_part",
            "Associated plant has respective plant part"
        ])

    # ------------------------------------------------------------
    # CREATE OUTPUT
    # ------------------------------------------------------------

    columns = [
        "Source",
        "Source Id",
        "Type",
        "Destination",
        "Destination Id",
        "Label",
        "Description"
    ]

    relationship_df = pd.DataFrame(
        relationships,
        columns=columns
    )

    relationship_df.to_excel(
        args.output,
        index=False
    )

    # ------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------

    print()
    print("=" * 60)
    print("HAS_PART COLLECTOR")
    print("=" * 60)

    print(f"Plants loaded:       {len(plant_df)}")
    print(f"Plant parts loaded:  {len(plantpart_df)}")
    print(f"Relationships made:  {len(relationship_df)}")

    print()
    print(f"Saved: {args.output}")
    print("=" * 60)


if __name__ == "__main__":
    main()
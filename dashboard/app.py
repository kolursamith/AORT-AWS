"""Optional Streamlit entry point for synthetic experiment output."""

from pathlib import Path


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise SystemExit("install the dashboard extra: pip install -e '.[dashboard]'") from exc
    st.title("AORT-AWS synthetic recovery dashboard")
    st.warning("Research simulator only — not connected to live banking systems.")
    result = Path("results/raw/hackathon.json")
    st.write(result.read_text() if result.exists() else "Run the experiment runner first.")


if __name__ == "__main__":
    main()

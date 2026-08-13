import asyncio
import os
import tempfile
from agent.routing.input_router import route_inputs
from agent.query import run_query

async def test_workflow():
    print("--- 1. Testing Input Router with MD and PDF ---")
    tmp = tempfile.mkdtemp()
    
    md_path = os.path.join(tmp, "test.md")
    with open(md_path, "w") as f:
        f.write("# Hello\nThis is a markdown file.")
        
    pdf_path = os.path.join(tmp, "test.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4\n%Fake PDF content for routing test\n")
        
    res = route_inputs([md_path, pdf_path])
    print(f"Context Files: {[f.filename for f in res.context_files]}")
    print(f"KB Files: {[f.filename for f in res.kb_files]}")
    
    print("\n--- 2. Testing End-to-End Query (Semantic) ---")
    try:
        query_result = await run_query("What is the capital of France?")
        print(f"Query: What is the capital of France?")
        print(f"Gate Mode: {query_result.gate_decision.mode}")
        print(f"Answer: {query_result.answer[:100]}...")
    except Exception as e:
        print(f"Query Error: {e}")
        
    print("\n--- 3. Testing PDF Ingest Fallback ---")
    try:
        from agent.knowledge.pdf_ingest import ingest_pdf
        await ingest_pdf(pdf_path=pdf_path)
        print("PDF Ingest: Succeeded (unexpected without PyMuPDF!)")
    except Exception as e:
        print(f"PDF Ingest Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_workflow())

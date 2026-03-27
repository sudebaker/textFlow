#!/usr/bin/env python3
"""
Generate comprehensive HTML API documentation for the project.

This script generates HTML documentation for:
- Go packages (internal/, pkg/, cmd/orchestrator/)
- Python workers (cmd/*-worker/)
"""

import os
import sys
import subprocess
import json
from pathlib import Path
from typing import Dict, List, Optional
import ast
import inspect


def generate_go_docs() -> None:
    """Generate Go documentation using go doc."""
    docs_dir = Path("docs/api")
    docs_dir.mkdir(parents=True, exist_ok=True)
    
    packages = [
        ("models", "./internal/models"),
        ("middleware", "./internal/middleware"),
        ("pipeline", "./internal/pipeline"),
        ("events", "./internal/events"),
        ("redis", "./internal/redis"),
        ("cache", "./internal/cache"),
        ("broker", "./internal/broker"),
        ("config", "./internal/config"),
        ("logging", "./pkg/logging"),
        ("metrics", "./pkg/metrics"),
    ]
    
    print("Generating Go documentation...")
    for pkg_short, pkg_path in packages:
        output_file = docs_dir / f"go_{pkg_short}.txt"
        
        try:
            result = subprocess.run(
                ["go", "doc", "-all", pkg_path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd="/home/hp/Proyectos/ia-text-ochestrator"
            )
            
            if result.returncode == 0 and result.stdout.strip():
                with open(output_file, "w") as f:
                    f.write(result.stdout)
                print(f"  ✓ {pkg_short}")
            else:
                # Fallback: read the files directly and create summary
                print(f"  ⚠️  {pkg_short}: Generating from source files...")
                generate_go_docs_from_source(Path(pkg_path), output_file)
        except Exception as e:
            print(f"  ✗ {pkg_short}: {e}")


def generate_go_docs_from_source(pkg_path: Path, output_file: Path) -> None:
    """Generate Go documentation by parsing source files."""
    go_files = list(pkg_path.glob("*.go"))
    if not go_files:
        return
    
    content = f"# Package Documentation: {pkg_path.name}\n\n"
    content += f"Location: {pkg_path}\n\n"
    
    for go_file in sorted(go_files):
        try:
            with open(go_file, "r") as f:
                lines = f.readlines()
            
            # Extract package doc and exported symbols
            in_comment = False
            buffer = []
            
            for line in lines:
                if line.strip().startswith("//"):
                    buffer.append(line)
                elif buffer and not line.strip().startswith("//"):
                    # Check if this is an exported symbol
                    if any(line.startswith(x) for x in ["type ", "func ", "const (", "var ("]):
                        content += f"## {go_file.name}\n"
                        content += "".join(buffer) + "\n"
                        content += line + "\n"
                    buffer = []
                    
        except Exception:
            pass
    
    with open(output_file, "w") as f:
        f.write(content if content != f"# Package Documentation: {pkg_path.name}\n\nLocation: {pkg_path}\n\n" else f"# {pkg_path.name}\n\nPackage documentation from source code.")


def extract_python_docstrings(file_path: Path) -> Dict:
    """Extract docstrings from a Python file."""
    try:
        with open(file_path, "r") as f:
            tree = ast.parse(f.read())
    except Exception as e:
        return {"error": str(e)}
    
    docs = {
        "file": str(file_path),
        "module_doc": ast.get_docstring(tree),
        "classes": {},
        "functions": {},
    }
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            docs["classes"][node.name] = {
                "docstring": ast.get_docstring(node),
                "methods": {},
                "lineno": node.lineno,
            }
            
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    docs["classes"][node.name]["methods"][item.name] = {
                        "docstring": ast.get_docstring(item),
                        "lineno": item.lineno,
                    }
        
        elif isinstance(node, ast.FunctionDef) and node.col_offset == 0:
            docs["functions"][node.name] = {
                "docstring": ast.get_docstring(node),
                "lineno": node.lineno,
            }
    
    return docs


def generate_python_docs() -> None:
    """Generate Python documentation."""
    docs_dir = Path("docs/api")
    docs_dir.mkdir(parents=True, exist_ok=True)
    
    workers = [
        "cmd/extraction-worker/worker.py",
        "cmd/completion-worker/worker.py",
        "cmd/metadata-worker/worker.py",
        "cmd/inference-worker/worker.py",
        "cmd/embeddings-worker/worker.py",
        "cmd/entities-worker/worker.py",
    ]
    
    print("\nGenerating Python documentation...")
    python_docs = {}
    
    for worker_path in workers:
        if Path(worker_path).exists():
            worker_name = Path(worker_path).parent.name
            docs = extract_python_docstrings(Path(worker_path))
            python_docs[worker_name] = docs
            print(f"  ✓ {worker_name}")
        else:
            print(f"  ✗ {worker_path}: Not found")
    
    # Save as JSON
    output_file = docs_dir / "python_docs.json"
    with open(output_file, "w") as f:
        json.dump(python_docs, f, indent=2)
    
    print(f"\nPython docs saved to {output_file}")


def generate_html_index() -> None:
    """Generate HTML index page."""
    docs_dir = Path("docs/api")
    docs_dir.mkdir(parents=True, exist_ok=True)
    
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IA Text Orchestrator - API Documentation</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            color: #333;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 40px 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
            padding: 40px;
        }
        h1 {
            color: #667eea;
            margin-bottom: 10px;
            font-size: 2.5em;
        }
        .subtitle {
            color: #666;
            margin-bottom: 40px;
            font-size: 1.1em;
        }
        .section {
            margin-bottom: 40px;
        }
        h2 {
            color: #764ba2;
            margin-top: 30px;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #667eea;
        }
        .doc-list {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }
        .doc-card {
            background: #f8f9fa;
            border-left: 4px solid #667eea;
            padding: 20px;
            border-radius: 4px;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .doc-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.2);
        }
        .doc-card h3 {
            color: #667eea;
            margin-bottom: 10px;
            font-size: 1.1em;
        }
        .doc-card p {
            color: #666;
            font-size: 0.95em;
            margin-bottom: 15px;
        }
        .doc-card a {
            display: inline-block;
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
            padding: 8px 16px;
            background: white;
            border: 1px solid #667eea;
            border-radius: 4px;
            transition: all 0.2s;
        }
        .doc-card a:hover {
            background: #667eea;
            color: white;
        }
        .status {
            display: inline-block;
            font-size: 0.85em;
            padding: 4px 12px;
            background: #e7f3ff;
            color: #0066cc;
            border-radius: 20px;
            margin-bottom: 10px;
        }
        .status.complete {
            background: #d4edda;
            color: #155724;
        }
        .status.partial {
            background: #fff3cd;
            color: #856404;
        }
        footer {
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #eee;
            color: #999;
            text-align: center;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📚 IA Text Orchestrator API Documentation</h1>
        <p class="subtitle">Event-driven microservices: Go orchestrator + Python workers</p>
        
        <div class="section">
            <h2>Go Packages</h2>
            <p>Backend orchestrator and infrastructure components.</p>
            <div class="doc-list">
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Models</h3>
                    <p>Core data structures: Job, Status, Task definitions</p>
                    <a href="go_models.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Middleware</h3>
                    <p>Circuit breaker, retry logic, error handling</p>
                    <a href="go_middleware.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Pipeline</h3>
                    <p>Orchestration engine, fan-out, polling</p>
                    <a href="go_pipeline.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Events</h3>
                    <p>Event bus, pub/sub, message routing</p>
                    <a href="go_events.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Redis</h3>
                    <p>Client, connection pooling, key operations</p>
                    <a href="go_redis.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Cache</h3>
                    <p>Content caching, cache-aside pattern</p>
                    <a href="go_cache.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Broker</h3>
                    <p>RabbitMQ integration, message queuing</p>
                    <a href="go_broker.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Config</h3>
                    <p>Configuration management, environment variables</p>
                    <a href="go_config.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Logging</h3>
                    <p>Structured logging with zerolog</p>
                    <a href="go_logging.txt">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Metrics</h3>
                    <p>Prometheus metrics collection</p>
                    <a href="go_metrics.txt">View Documentation →</a>
                </div>
            </div>
        </div>
        
        <div class="section">
            <h2>Python Workers</h2>
            <p>Background processing workers for specialized tasks.</p>
            <div class="doc-list">
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Extraction Worker</h3>
                    <p>Document extraction via Docling, source classification</p>
                    <a href="python_extraction_docs.html">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Completion Worker</h3>
                    <p>LLM aggregation, final output generation, webhooks</p>
                    <a href="python_completion_docs.html">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Metadata Worker</h3>
                    <p>Lightweight metadata extraction, language detection</p>
                    <a href="python_metadata_docs.html">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Inference Worker</h3>
                    <p>LLM integration, model discovery, fallback logic</p>
                    <a href="python_inference_docs.html">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Embeddings Worker</h3>
                    <p>BAAI/bge-m3 embeddings, vector generation</p>
                    <a href="python_embeddings_docs.html">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Entities Worker</h3>
                    <p>GLiNER NER (Named Entity Recognition) offline</p>
                    <a href="python_entities_docs.html">View Documentation →</a>
                </div>
            </div>
        </div>
        
        <div class="section">
            <h2>Architecture</h2>
            <p>System design, deployment, and integration guides.</p>
            <div class="doc-list">
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>System Overview</h3>
                    <p>High-level architecture, data flows, deployment models</p>
                    <a href="../README_ARCHITECTURE.md">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Go Infrastructure</h3>
                    <p>Internal packages, Redis schema, middleware patterns</p>
                    <a href="../internal/README_ARCHITECTURE.md">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Workers & Services</h3>
                    <p>Worker lifecycle, RabbitMQ queues, error handling</p>
                    <a href="../cmd/README_ARCHITECTURE.md">View Documentation →</a>
                </div>
                <div class="doc-card">
                    <span class="status complete">Complete</span>
                    <h3>Shared Libraries</h3>
                    <p>Common utilities, logging, metrics, event definitions</p>
                    <a href="../pkg/README_ARCHITECTURE.md">View Documentation →</a>
                </div>
            </div>
        </div>
        
        <footer>
            <p>Generated on 2026-03-27 | <a href="https://github.com/anomalyco/ia-text-orchestrator">Repository</a></p>
        </footer>
    </div>
</body>
</html>
"""
    
    index_file = docs_dir / "index.html"
    with open(index_file, "w") as f:
        f.write(html_content)
    
    print(f"HTML index generated: {index_file}")


def generate_python_html_docs() -> None:
    """Generate detailed HTML documentation for Python workers."""
    docs_dir = Path("docs/api")
    docs_dir.mkdir(parents=True, exist_ok=True)
    
    workers = [
        ("extraction-worker", "cmd/extraction-worker/worker.py"),
        ("completion-worker", "cmd/completion-worker/worker.py"),
        ("metadata-worker", "cmd/metadata-worker/worker.py"),
        ("inference-worker", "cmd/inference-worker/worker.py"),
        ("embeddings-worker", "cmd/embeddings-worker/worker.py"),
        ("entities-worker", "cmd/entities-worker/worker.py"),
    ]
    
    print("\nGenerating Python HTML documentation...")
    
    for worker_name, worker_path in workers:
        if not Path(worker_path).exists():
            print(f"  ⚠️  {worker_name}: File not found")
            continue
        
        docs = extract_python_docstrings(Path(worker_path))
        
        html = generate_python_doc_html(worker_name, docs)
        
        output_file = docs_dir / f"python_{worker_name}_docs.html"
        with open(output_file, "w") as f:
            f.write(html)
        
        print(f"  ✓ {worker_name}")


def generate_python_doc_html(worker_name: str, docs: Dict) -> str:
    """Generate HTML documentation for a Python worker."""
    
    module_doc = docs.get("module_doc", "No documentation")
    classes = docs.get("classes", {})
    functions = docs.get("functions", {})
    
    classes_html = ""
    for class_name, class_info in classes.items():
        class_doc = class_info.get("docstring", "No documentation")
        methods = class_info.get("methods", {})
        
        methods_html = ""
        for method_name, method_info in methods.items():
            method_doc = method_info.get("docstring", "No documentation")
            methods_html += f"""
                <div class="method">
                    <h4>{method_name}</h4>
                    <p>{method_doc or "No documentation"}</p>
                </div>
            """
        
        classes_html += f"""
        <div class="class-section">
            <h3 class="class-name">{class_name}</h3>
            <p class="docstring">{class_doc or "No documentation"}</p>
            {f'<div class="methods">{methods_html}</div>' if methods_html else ''}
        </div>
        """
    
    functions_html = ""
    for func_name, func_info in functions.items():
        func_doc = func_info.get("docstring", "No documentation")
        functions_html += f"""
        <div class="function">
            <h3>{func_name}</h3>
            <p>{func_doc or "No documentation"}</p>
        </div>
        """
    
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{worker_name} - API Documentation</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}
        .container {{
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
            padding: 40px;
        }}
        h1 {{
            color: #667eea;
            margin-bottom: 10px;
            font-size: 2em;
        }}
        h2 {{
            color: #764ba2;
            margin-top: 30px;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #667eea;
        }}
        h3 {{
            color: #667eea;
            margin-top: 20px;
            margin-bottom: 10px;
            font-size: 1.2em;
        }}
        h4 {{
            color: #555;
            margin-top: 15px;
            margin-bottom: 5px;
            font-family: 'Monaco', 'Courier New', monospace;
            font-weight: 600;
        }}
        .docstring {{
            background: #f8f9fa;
            padding: 15px;
            border-left: 4px solid #667eea;
            border-radius: 4px;
            margin: 15px 0;
            white-space: pre-wrap;
            word-break: break-word;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.95em;
            line-height: 1.5;
            color: #333;
        }}
        .class-section {{
            margin: 30px 0;
            padding: 20px;
            background: #fafbfc;
            border-left: 4px solid #764ba2;
            border-radius: 4px;
        }}
        .class-name {{
            font-family: 'Monaco', 'Courier New', monospace;
            font-weight: 700;
            color: #764ba2;
        }}
        .method {{
            margin: 15px 0 15px 20px;
            padding: 10px;
            background: white;
            border-radius: 4px;
            border-left: 3px solid #667eea;
        }}
        .method h4 {{
            margin-top: 0;
            color: #667eea;
        }}
        .function {{
            margin: 20px 0;
            padding: 15px;
            background: #f0f7ff;
            border-left: 4px solid #0066cc;
            border-radius: 4px;
        }}
        .function h3 {{
            margin-top: 0;
            color: #0066cc;
        }}
        .back-link {{
            display: inline-block;
            margin-bottom: 20px;
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
        }}
        .back-link:hover {{
            text-decoration: underline;
        }}
        footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #eee;
            color: #999;
            text-align: center;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="container">
        <a href="index.html" class="back-link">← Back to Documentation</a>
        
        <h1>🐍 {worker_name.replace('-', ' ').title()}</h1>
        
        <h2>Module Documentation</h2>
        <p class="docstring">{module_doc or "No documentation"}</p>
        
        {f'<h2>Classes</h2>{classes_html}' if classes_html else ''}
        {f'<h2>Functions</h2>{functions_html}' if functions_html else ''}
        
        <footer>
            <p>Generated on 2026-03-27</p>
        </footer>
    </div>
</body>
</html>
"""
    
    return html_content


if __name__ == "__main__":
    print("🚀 Generating API Documentation...\n")
    
    generate_go_docs()
    generate_python_docs()
    generate_python_html_docs()
    generate_html_index()
    
    print("\n✅ Documentation generation complete!")
    print("📄 Open docs/api/index.html to view the documentation")

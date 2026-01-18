#!/usr/bin/env python3
"""
Validation script to verify all components are in place.
Run: python validate.py
"""

import os
import sys
from pathlib import Path

def check_file(path, name):
    if Path(path).exists():
        print(f"✅ {name}")
        return True
    else:
        print(f"❌ {name} - NOT FOUND")
        return False

def check_python_imports():
    """Verify Python modules can be imported."""
    try:
        from api.job_manager import job_manager
        from api.main import app
        from demo.worker import main
        print("✅ Python imports successful (s3_utils gracefully handles missing boto3)")
        return True
    except Exception as e:
        print(f"❌ Python imports failed: {e}")
        return False

def check_python_syntax():
    """Verify Python syntax is correct."""
    import py_compile
    files = [
        "api/job_manager.py",
        "api/main.py",
        "demo/worker.py",
        "demo/coordinator.py",
        "demo/s3_utils.py",
    ]
    all_ok = True
    for f in files:
        try:
            py_compile.compile(f, doraise=True)
            print(f"✅ Syntax OK: {f}")
        except Exception as e:
            print(f"❌ Syntax error in {f}: {e}")
            all_ok = False
    return all_ok

def main():
    print("=" * 60)
    print("VALIDATION SCRIPT - Distributed Training System")
    print("=" * 60)
    
    checks = []
    
    print("\n📁 File Structure:")
    files = [
        ("api/main.py", "API server"),
        ("api/job_manager.py", "Job manager with summary/purge"),
        ("demo/worker.py", "Worker with heartbeat + S3"),
        ("demo/coordinator.py", "Python coordinator (legacy)"),
        ("demo/s3_utils.py", "S3/MinIO utilities"),
        ("requirements.txt", "Python dependencies"),
        ("Dockerfile.worker", "Worker image"),
        ("Dockerfile.api", "API image"),
        ("Dockerfile.coordinator", "Coordinator image"),
        ("docker-compose.yml", "Docker Compose"),
        ("coordinator/Cargo.toml", "Rust project"),
        ("coordinator/src/main.rs", "Rust coordinator"),
        ("k8s/01-namespace-config.yaml", "K8s namespace"),
        ("k8s/02-minio.yaml", "K8s MinIO"),
        ("k8s/03-coordinator.yaml", "K8s coordinator"),
        ("k8s/04-api.yaml", "K8s API"),
        ("QUICKSTART.md", "Quick start guide"),
        ("BUILD_DEPLOY.md", "Build & deploy guide"),
        ("IMPLEMENTATION.md", "Implementation summary"),
    ]
    
    for path, name in files:
        checks.append(check_file(path, name))
    
    print("\n🔧 Python Validation:")
    checks.append(check_python_syntax())
    checks.append(check_python_imports())
    
    print("\n📦 Dependencies Check:")
    try:
        import fastapi
        import pydantic
        import psutil
        print("✅ FastAPI installed")
        print("✅ Pydantic installed")
        print("✅ psutil installed")
    except ImportError as e:
        print(f"❌ Missing dependencies: {e}")
        print("   Run: pip install -r requirements.txt")
        checks.append(False)
    
    try:
        import boto3
        print("✅ boto3 installed (S3 support)")
    except ImportError:
        print("⚠️  boto3 not installed (optional for local testing)")
    
    print("\n" + "=" * 60)
    if all(checks):
        print("✅ ALL CHECKS PASSED - Ready to run!")
        print("\nNext steps:")
        print("1. Local test: python demo/coordinator.py")
        print("2. API test: uvicorn api.main:app --reload")
        print("3. Docker test: docker-compose up -d")
        print("4. K8s test: kubectl apply -f k8s/")
        return 0
    else:
        failed = sum(1 for c in checks if not c)
        print(f"❌ {failed} CHECK(S) FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(main())

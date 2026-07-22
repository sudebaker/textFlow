"""
Setup configuration for worker_common library.

This package provides shared utilities for Python workers in the textFlow project.
"""

from setuptools import setup, find_packages

setup(
    name="worker-common",
    version="1.0.0",
    description="Shared utilities for textFlow Python workers",
    author="textFlow Team",
    packages=find_packages(where="."),
    package_dir={"": "."},
    python_requires=">=3.9",
    install_requires=[
        "pika>=1.3.0",
        "prometheus-client>=0.16.0",
        "requests>=2.28.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)

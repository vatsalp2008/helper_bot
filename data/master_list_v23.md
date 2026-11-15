# COMPLETE PROJECT MASTER LIST
## Vatsal Patel - Portfolio Projects & Professional Experience

**Last Updated:** November 2026
**Total Projects:** 26
**Total Professional Experience:** 6 positions
**GitHub Profile:** https://github.com/vatsalp2008

---

## TABLE OF CONTENTS

1. [All Projects Overview](#all-projects-overview)
2. [Project Details](#project-details)
3. [Professional Experience](#professional-experience)
4. [Summary Statistics](#summary-statistics)
5. [GitHub Links Quick Reference](#github-links-quick-reference)
6. [Skills & Technologies](#skills--technologies)
7. [Career Positioning](#career-positioning)
8. [Portfolio Highlights](#portfolio-highlights)

---

## ALL PROJECTS OVERVIEW

| # | Project Name | Category | Timeline | Status |
|---|-------------|----------|----------|--------|
| 1 | StreamLine | Distributed Systems | Dec 2025 - Jan 2026 | Complete |
| 2 | E-commerce Platform | Microservices | Jun 2024 - Aug 2024 | Complete |
| 3 | Intelligent Chess AI | Agentic AI | Sep 2025 - Nov 2025 | Complete |
| 4 | AI Agent Discovery | Agentic AI | Nov 2025 - Dec 2025 | Complete |
| 5 | F1 Analytics Dashboard | ML + Visualization | Jun 2025 - Oct 2025 | Complete |
| 6 | Investment Prediction | ML/Deep Learning | Jan 2025 - May 2025 | Complete |
| 7 | E-commerce Recommendation | ML/RecSys | Jun 2024 - Aug 2024 | Complete |
| 8 | NutriTrack | Full-Stack Web | Dec 2025 - Jan 2026 | Complete |
| 9 | Seattle Transit | Algorithms | Sep 2025 - Oct 2025 | Complete |
| 10 | Housing Price Prediction | ML/Regression | Sep 2024 - Dec 2024 | Complete |
| 11 | MoodMeals | AI + PWA | Mar 2026 - Apr 2026 | Complete |
| 12 | Traffic Accident Severity | ML/Classification | Mar 2026 - Apr 2026 | Complete |
| 13 | theManager (Decidr Code) | Developer Tools | Apr 2026 - May 2026 | Complete |
| 14 | FORUM | Civic Tech Research | Apr 2026 - May 2026 | Complete |
| 15 | SWARM | Agentic Platform | Apr 2026 - May 2026 | Complete |
| 16 | Wardrobe | iOS Native App | Jun 2026 - Jul 2026 | Complete |
| 17 | PulseTrader | Real-time Streaming | Jun 2026 - Jul 2026 | Complete |
| 18 | ScenarioForge | AV Simulation | Jun 2026 - Jul 2026 | Complete |
| 19 | PerceptNet | AV Perception | Jun 2026 - Jul 2026 | Complete |
| 20 | TransitLens | Android Accessibility | Jun 2026 - Jul 2026 | Complete |
| 21 | ExperimentIQ | Data/Analytics Platform | Jul 2026 - Aug 2026 | Complete |
| 22 | Open Source: spiceai/spiceai | Distributed Data Systems | Jul 2026 - Present | Merged (PR #13507) |
| 23 | Open Source: chef/chef | Configuration Management | Aug 2026 - Present | Merged (2026-08-31) |
| 24 | EchoCoach | Voice AI / Agentic AI | Jun 2026 - Jul 2026 | Complete |
| 25 | Afterglow | Computer Vision / Real-Time Graphics | Sep 2026 - Oct 2026 | Complete |
| 26 | PNWER Trade & Tariffs: Cross-Border Trade & Tariff Explorer | Data/Policy Analytics Platform | Sep 2026 - Dec 2026 | In Progress (Experience Expo) |

---

## PROJECT DETAILS

### PROJECT 1: StreamLine - Distributed Messaging Engine
**Timeline:** December 2025 - January 2026
**GitHub:** https://github.com/vatsalp2008/StreamLine-Distributed-Messaging-Engine
**Technologies:** Java 21, Spring Boot, WebSocket, JPA, H2 Database, Maven, Async Processing

**Description:**
- Built a high-performance distributed messaging system in Java 21 and Spring Boot with real-time WebSocket communication and event-driven architecture
- Implemented asynchronous persistence via JPA with an H2 database to decouple message storage from real-time operations
- Designed thread-safe concurrent operations using ConcurrentHashMap and CopyOnWriteArrayList for high-concurrency scenarios
- Built load testing and latency analysis tooling to measure p99 latency and system throughput
- Added automatic message history replay (last 50 messages) for context on user join

**Key Metrics:** Low-latency WebSocket messaging | p99 latency testing | Thread-safe concurrent operations | Automatic history replay (50 messages)

---

### PROJECT 2: E-commerce Platform (Microservices Architecture)
**Timeline:** June 2024 - August 2024
**GitHub:** https://github.com/vatsalp2008/E-commerce-Platform
**Technologies:** Java 17, Spring Boot 3.2, React, TypeScript, Go, Kafka, Docker, Kubernetes, Terraform, AWS (ECS, RDS, VPC, IAM), Azure (Cosmos DB), Sentry, CloudWatch, Microservices, Event-Driven Architecture

**Description:**
- Architected a production-grade, event-driven e-commerce platform with 9 independent microservices (API Gateway, Product, Inventory, User, Cart, Order, Payment, Notification, plus a Go-based service), each with its own dedicated database
- The Notification service is specifically built in Go, distinguishing it from the Java-based core services
- Implemented a choreography-based Saga pattern for distributed transactions and eventual consistency across services
- Used a transactional outbox pattern with Kafka event streaming for reliable inter-service communication
- Built a comprehensive React and TypeScript frontend with responsive design
- Containerized the system with Docker Compose and added Prometheus/Grafana for observability
- Can also be described as deployed on Kubernetes (in addition to Docker Compose), depending on the job description
- Wrote Terraform modules provisioning production AWS infrastructure: an RDS instance per microservice (matching the database-per-service pattern), a VPC with segmented subnets/security groups, an ECS cluster, and scoped IAM roles per service — turning the manually-configured stack into reproducible, declarative infrastructure
- Added Sentry and CloudWatch alongside the existing Prometheus/Grafana monitoring stack for error tracking and infrastructure health visibility
- Migrated one service's database from PostgreSQL to Cosmos DB (Azure's NoSQL offering), comparing relational and non-relational data modeling within the same database-per-service architecture

**Key Metrics:** 9 independent microservices (1 in Go) | Choreography-based Sagas | Kafka event streaming | JWT authentication | Kubernetes-deployable | Terraform-provisioned AWS infra (RDS, VPC, ECS, IAM) | Sentry + CloudWatch monitoring | Cosmos DB (Azure NoSQL) migration for one service

---

### PROJECT 3: Intelligent Chess AI Platform
**Timeline:** September 2025 - November 2025
**GitHub:** https://github.com/vatsalp2008/Intelligent-Chess-AI
**Technologies:** Python, Ollama, llama3.2, mistral, Minimax, Alpha-Beta Pruning, Prompt Engineering, Flask

**Description:**
- Classical Engine (Knightmare): Minimax with Alpha-Beta pruning, 100% win rate vs random opponents, 70% vs 1400-1600 ELO, 8-ply search with iterative deepening, killer moves heuristic, history heuristic, quiescence search
- LLM-Powered Agent (ChessGPT): local LLMs via Ollama (Llama 3.2, Mistral), 85.7% accuracy improvement through iterative optimization, retry logic, recovery mechanisms
- Web-based interface for human play and tournament mode for performance evaluation

**Key Metrics:** 100% win rate (classical) | 85.7% accuracy improvement (LLM agent) | 8-ply search depth

---

### PROJECT 4: AI Agent Discovery System
**Timeline:** November 2025 - December 2025
**GitHub:** https://github.com/vatsalp2008/AI-Agent-Discovery
**Technologies:** Python, Ollama, LangChain, n8n, Flask

**Description:**
- Built a RAG discovery platform helping developers find and compare AI agents across platforms
- Data collection pipeline gathering info on 100+ AI agents from GitHub, Hugging Face Spaces, LangChain docs
- Semantic search via LangChain, local Ollama embeddings, ChromaDB vector storage
- Multi-agent workflow orchestration via n8n for automated data collection
- Flask REST API and web interface with category filtering, popularity sorting

**Key Metrics:** 100+ AI agents indexed | Semantic search with embeddings | RAG architecture

---

### PROJECT 5: F1 Analytics Dashboard - Prediction & Telemetry Visualization
**Timeline:** June 2025 - October 2025
**GitHub:** https://github.com/vatsalp2008/f1-analytics-dashboard
**Technologies:** Python, Scikit-learn, XGBoost, TensorFlow, Spark, Pandas, React.js, D3.js, Power BI, Flask, FastF1, Arcade, NumPy, Tkinter

**Description:**
- Trained XGBoost/TensorFlow models: 73% podium accuracy, 3.2s MAE on lap times, across 500K+ historical data points, 10+ seasons, processed via Apache Spark
- Interactive React.js/D3.js dashboard with race predictions, confidence intervals, driver comparisons
- Built a genuine Power BI report as an additional reporting layer alongside the React/D3.js dashboard, for business/analyst-facing contexts
- Real-time race replay via FastF1 API: live position tracking, dynamic leaderboards, full telemetry
- Playback controls (0.5x-4x speed, frame-by-frame), GUI session selector spanning 2018-2025

**Key Metrics:** 73% podium accuracy | 3.2s MAE | 500K+ data points | 70+ features | 10+ seasons | Power BI reporting layer

---

### PROJECT 6: Investment Prediction Platform
**Timeline:** January 2025 - May 2025
**GitHub:** https://github.com/vatsalp2008/Investment-Prediction
**Technologies:** Python, TensorFlow, PyTorch, LSTM/GRU, Pandas, Flask, PostgreSQL, MongoDB, SQL

**Description:**
- Time-series forecasting platform using LSTM/GRU on 1.2M data points spanning 10+ years
- 68% directional accuracy with robust backtesting across market conditions
- Optimized feature engineering pipeline, reducing training time 60% via batch processing/caching
- Production Flask REST API, PostgreSQL for structured data, MongoDB for unstructured sentiment data

**Key Metrics:** 68% directional accuracy | 1.2M data points | 60% training time reduction

---

### PROJECT 7: E-commerce Recommendation Research Engine
**Timeline:** June 2024 - August 2024
**GitHub:** https://github.com/vatsalp2008/E-commerce-recommendation-system
**Technologies:** Python, Scikit-learn, Pandas, NumPy, Scipy, Joblib, Matrix Factorization, Collaborative Filtering

**Description:**
- Hybrid recommendation system: content-based + collaborative filtering via matrix factorization
- Precision@10 0.64, Recall@10 0.76, MAP 0.59 on evaluation datasets
- Modular architecture (data loading, preprocessing, training, evaluation) with benchmarking suite

**Key Metrics:** Precision@10: 0.64 | Recall@10: 0.76 | MAP: 0.59

---

### PROJECT 8: NutriTrack - Food Tracking Application
**Timeline:** December 2025 - January 2026
**GitHub:** https://github.com/vatsalp2008/Food-Tracker
**Technologies:** React 19, Vite, JavaScript, CSS, Lucide React, localStorage

**Description:**
- Food tracking web app with glassmorphism UI, React 19 and Vite
- Food logging (name, category, nutritional values), real-time nutritional dashboard
- Category-based filtering, localStorage persistence, responsive design, micro-interactions

**Key Features:** Real-time dashboard | Category filtering | Glassmorphism UI | localStorage persistence

**Note:** This project's frontend/backend framing has been described differently across applications to match each job's stated stack, all built on the same real project. Confirmed variants: React (original), Angular/TypeScript (for Garmin), and Vue.js + Django + GitLab CI (for PathAI). Confirm current live implementation with Vatsal before reusing any specific framing.

---

### PROJECT 9: Seattle Transit Pathfinding System
**Timeline:** September 2025 - October 2025
**GitHub:** https://github.com/vatsalp2008/seattle-transit-pathfinder
**Technologies:** Python, C++, Rust, TypeScript, PostgreSQL, A*, BFS, Dijkstra, Docker, NetworkX, REST APIs

**Description:**
- Graph-based route optimization for Seattle's King County Metro transit network
- A*, BFS, Dijkstra compared across 15,000+ transit stops using NetworkX
- 35% computation time reduction via algorithm/data structure optimization
- Rewrote a performance-critical scoring component in Rust for additional speed and memory safety, alongside the existing C++ implementation
- Flask REST API handling 1000+ queries/second, sub-second response times, backed by PostgreSQL
- Containerized with Docker; C++ performance-critical components with Python bindings

**Key Metrics:** 15,000+ stops | 35% computation reduction | 1000+ queries/sec | Rust scoring component

---

### PROJECT 10: Housing Price Prediction System
**Timeline:** September 2024 - December 2024
**GitHub:** https://github.com/vatsalp2008/Housing-Price-Prediction
**Technologies:** Python, Scikit-learn, XGBoost, Random Forest, Gradient Boosting, Pandas, R, Flask, PostgreSQL

**Description:**
- Ensemble regression (XGBoost, Random Forest, Gradient Boosting) with hyperparameter tuning/cross-validation
- R² 0.82, RMSE $45,000, MAE $32,000 on 50,000+ property records
- 30+ engineered features; preprocessing pipeline for missing values, encoding, scaling
- Flask web interface, PostgreSQL backend, R-based statistical validation

**Key Metrics:** R² = 0.82 | 50K+ records | 30+ features | $45K RMSE

---

### PROJECT 11: MoodMeals - Emotion-Aware Meal Planning PWA
**Timeline:** March 2026 - April 2026
**GitHub:** https://github.com/vatsalp2008/MoodMeals
**Technologies:** Next.js, React, TypeScript, Claude API, FastAPI, Supabase, Spoonacular API, PWA

**Description:**
- PWA recommending meals based on emotional state, grounded in nutritional psychiatry research
- Multi-modal mood input: text (Claude API), sliders, voice-to-mood detection
- Pantry-aware, seasonally-optimized recommendations; post-meal mood tracking over time
- Next.js/TypeScript frontend, FastAPI backend, Supabase persistence, Spoonacular recipe data
- Smart grocery planning, offline-capable installable PWA

**Key Features:** Multi-modal mood input | Mood-to-nutrient mapping | PWA with offline capability

---

### PROJECT 12: Traffic Accident Severity Prediction
**Timeline:** March 2026 - April 2026
**GitHub:** https://github.com/vatsalp2008/Accidents-Severity
**Technologies:** Python, XGBoost, Scikit-learn, SMOTE, SHAP, Pandas, Seaborn, Folium, Tableau, Kaggle API

**Description:**
- Binary classification (minor vs severe) on 7.7M US accident records, 49 states, 2016-2023
- Stratified 10% sample (772,840 rows); preprocessing with leakage detection, imputation, weather bucketing
- Addressed class imbalance (80.5%/19.5%) via SMOTE and balanced class weights
- Benchmarked 3 baselines, tuned XGBoost via GridSearchCV targeting Recall >0.70, F1 >0.65
- SHAP explainability, Folium geographic heatmap validated against WSDOT data
- Built a Tableau Public dashboard translating model findings into a visual narrative for non-technical stakeholders

**Key Metrics:** 7.7M records | 772K sample | ROC-AUC 0.818 | Tableau Public dashboard

---

### PROJECT 13: theManager (Decidr Code) - AI-Powered Software Factory
**Timeline:** April 2026 - May 2026
**GitHub:** https://github.com/aditya-pandita/theManager
**Technologies:** TypeScript, React, Tauri v2 (Rust), Express, PostgreSQL, Drizzle ORM, Gemma 4, MCP, GraphQL, Zustand, Vite, Tailwind CSS, QuickBooks API

**Description:**
- Multi-project workspace transforming specs into executable code via 7-agent AI pipeline (planner, architect, coder, tester, reviewer, debugger, docs)
- Ingests PRDs/SRS via Gemma 4, auto-generates kanban boards with structured user stories
- 4 auto-derived flow diagrams (Architecture, Dataflow, Lifecycle, Reasoning) computed live, zero manual maintenance
- MCP integration: 11 tools, 3 resources, 2 prompts; editor-native access via Cursor, Claude Code, Windsurf
- Added a GraphQL API layer alongside existing REST/MCP endpoints
- Added a QuickBooks API integration for automated financial data sync (built for Beacon Software application; confirm current status before reuse)
- Tauri v2 desktop app (5-10MB vs 150MB+ Electron), 7-package monorepo, real-time SSE updates

**Key Components:** 7-agent pipeline | MCP (11 tools/3 resources/2 prompts) | REST + GraphQL | QuickBooks integration | 4 auto-derived diagrams

---

### PROJECT 14: FORUM - Multi-Agent Civic Deliberation Simulator
**Timeline:** April 2026 - May 2026
**GitHub:** https://github.com/vatsalp2008/Forum
**Technologies:** Python, LangGraph, DuckDB/SQLite, Gemini API (Gemma 4), ACS PUMS, ANES, Ollama, uv/poetry

**Description:**
- Open-source research artifact simulating civic deliberation via demographically-grounded AI agents
- Fishkin's Deliberative Polling protocol as LangGraph node graph
- Synthetic agents from ACS PUMS census data (k-anonymity ≥5), seeded with ANES 2020 priors
- Backtest harness vs resolved WA ballot measures (I-1631, I-1639), Brier scores/MAE/per-segment error
- Transparency architecture: audit trails, documented biases, confidence intervals; targeted for *Political Analysis* journal

**Key Features:** Deliberative Polling | Demographically-grounded personas | Backtest harness | k-anonymity ≥5

---

### PROJECT 15: SWARM - Asynchronous Parallel AI Agent Platform
**Timeline:** April 2026 - May 2026
**GitHub:** https://github.com/vatsalp2008/Swarm
**Technologies:** TypeScript, LangGraph.js, Next.js 15, React 19, Supabase, Stagehand, Playwright, Browserbase, Gemini API, Drizzle ORM, pnpm 9, Turborepo 2, Fly.io, Vercel, Tailwind CSS 3, TanStack Query, Zod, Stripe API, OIDC

**Description:**
- Asynchronous, parallel, authentication-aware AI agent platform: specialized "Bees" in isolated browser sandboxes with human review gates
- Monorepo: 7 packages + 2 apps (shared types, DB schema, audit logging, token vault, Bee SDK; Next.js 15 frontend; LangGraph.js orchestrator with Postgres LISTEN/NOTIFY)
- Added a Stripe API integration for automated billing workflows (built for Beacon Software application; confirm current status before reuse)
- Extended the existing OAuth token vault to support OIDC-based SSO for third-party integrations, building on Supabase Auth's native OIDC provider support
- 5 Architecture Decision Records; user-as-principal legal posture with permanent deny-list
- Security: PII redaction, OAuth token vault (per-Bee scope isolation), default-deny compliance architecture, SHA-256 hash-chain audit logging
- 18 distinct audit event types

**Key Metrics:** 7-package monorepo | 18 audit event types | <$5 per 12-agent task | Stripe billing integration | OAuth 2.0 + OIDC SSO support

---

### PROJECT 16: Wardrobe - AI-Powered Personal Styling App
**Timeline:** June 2026 - July 2026
**GitHub:** https://github.com/vatsalp2008/Wardrobe
**Technologies:** Swift 5.9, SwiftUI, UIKit, Vision (VisionKit), Core ML, Core Data, WeatherKit, PhotosUI, AVFoundation, Combine, CryptoKit, UserNotifications, Claude API, IDM-VTON (Replicate), Remove.bg API, SerpAPI, CLIP Embeddings, Supabase, WebSocket, gRPC

**Description:**
- Native iOS app digitizing a user's clothing collection, AI-generated outfit recommendations
- Computer-vision closet scanner: Apple Vision + Core ML for on-device segmentation, background removal, classification (6 categories)
- Combined SwiftUI declarative views with UIKit-based interactive animations and gesture-driven transitions for outfit browsing flows
- AI outfit generation engine (Claude API + WeatherKit), weekly-refreshing trend scores
- Virtual try-on via IDM-VTON (Replicate API) with Vision-based pose validation, result caching
- Gap Finder algorithm for highest-impact wardrobe purchases, live shopping via SerpAPI
- Architected a dual networking layer over WebSockets (real-time sync) and gRPC (structured backend calls)
- MVVM + Repository pattern, Core Data + Supabase cloud sync, CryptoKit AES-GCM encryption

**Key Metrics:** 85% reduction in manual tagging effort | 100% cut in repeat try-on API costs | Dual WebSocket + gRPC networking

---

### PROJECT 17: PulseTrader - Real-Time Financial Market Streaming Pipeline
**Timeline:** June 2026 - July 2026
**GitHub:** https://github.com/vatsalp2008/PulseTrader
**Technologies:** Python, Apache Kafka, Apache Flink, Confluent Schema Registry (Avro), TimescaleDB, BigQuery, Redis, FastAPI, WebSocket, React 19, TypeScript, Recharts, D3.js, Zustand, Tailwind CSS, Docker, Prometheus, Grafana

**Description:**
- End-to-end streaming pipeline for real-time financial trade processing (Bloomberg/Robinhood/Citadel/Stripe-style architecture)
- 10,000 trade events/sec across 8 symbols via Kafka with Avro schema evolution (Confluent Schema Registry)
- 3 Flink jobs: normalization, tumbling-window aggregation (VWAP, buy/sell ratio, z-scores), anomaly detection; exactly-once semantics
- Watermark-based out-of-order handling, 10s anomaly suppression window, dead letter queues
- TimescaleDB hypertables + continuous aggregates; FastAPI/WebSocket backend; React dashboard
- Migrated analytical storage layer to BigQuery for scalable historical/batch querying, while retaining TimescaleDB continuous aggregates for sub-100ms real-time serving

**Key Metrics:** 10,000 events/sec | p99 <50ms | Exactly-once semantics | Sub-100ms real-time queries | BigQuery migration for batch/historical

---

### PROJECT 18: ScenarioForge - Scenario-Based Perception Evaluation & Simulation Framework
**Timeline:** June 2026 - July 2026
**GitHub:** https://github.com/vatsalp2008/ScenarioForge
**Technologies:** Python, CARLA 0.9.15, OpenSCENARIO 2.0, PyTorch, NumPy, Matplotlib, Seaborn, Plotly, ROS 2, PostgreSQL, Ray, Docker, MLflow, GitHub Actions

**Description:**
- Adversarial scenario generation + closed-loop perception evaluation for AV safety validation
- 50+ scenario library across 8 categories, executed in CARLA 0.9.15 with OpenSCENARIO 2.0, full sensor suite
- Physics-grounded sensor noise models calibrated to real hardware specs
- Closed-loop evaluation coupling detection to a PID controller, measuring collision/near-miss rates
- Evaluation engine: IoU matching, SOTIF-aligned failure taxonomy, PostgreSQL results DB, Ray parallel execution

**Key Metrics:** 50+ scenarios/8 categories | 23% mAP degradation in fog | 3 critical closed-loop failure modes | SOTIF-aligned

---

### PROJECT 19: PerceptNet - Multi-Modal Camera + LiDAR Fusion Perception Pipeline
**Timeline:** June 2026 - July 2026
**GitHub:** https://github.com/vatsalp2008/PerceptNet
**Technologies:** Python, PyTorch 2.2, YOLO v8, PointPillars, KITTI, nuScenes, TensorRT 8.6, ONNX, CUDA, ROS 2 Humble, Open3D, OpenCV, Docker, MLflow

**Description:**
- Multi-modal perception fusing RGB camera + LiDAR for real-time 3D detection, classification, multi-object tracking
- YOLO v8 + FPN camera branch + PointPillars LiDAR branch, fused via extrinsic calibration and MLP fusion head
- The real-time 3D detection path specifically uses CUDA-accelerated inference to meet real-time latency constraints
- AB3DMOT tracker (3D Kalman filter + Hungarian algorithm) with persistent track IDs
- Benchmarked on KITTI (7,481 frames) + nuScenes; TensorRT INT8 quantization via ONNX export
- ROS 2 Humble node publishing Detection3DArray messages

**Key Metrics:** ~77% Car mAP@0.7 on KITTI | MOTA >75% | ~22ms/frame (>40 FPS) via TensorRT INT8 + CUDA | <8% mAP drop under LiDAR dropout

---

### PROJECT 20: TransitLens - Multimodal Transit Assistant for Accessible Urban Navigation
**Timeline:** June 2026 - July 2026
**GitHub:** https://github.com/vatsalp2008/TransitLens
**Technologies:** Kotlin 1.9, Jetpack Compose, CameraX, TensorFlow Lite, ML Kit Text Recognition v2, GTFS-RT, Android Accessibility Services/TalkBack, Room, DataStore, Hilt, ARCore, OpenStreetMap Overpass API

**Description:**
- On-device multimodal ML assistant for accessible urban transit navigation, Android 10+, offline-capable core
- On-device scene understanding: MobileNetV3-Small TFLite classifier (<50ms), ML Kit text recognition, TFLite object detector
- Accessibility-aware router: constraint-weighted Dijkstra over GTFS + OpenStreetMap data (avoid stairs, steep grades, elevator-required)
- 10-pattern haptic vibration language, TTS, minimal AR overlay; King County Metro GTFS-RT integration (30s refresh, offline fallback)
- WCAG 2.1 AA compliance from the start: TalkBack, 4.5:1 contrast, switch access, icon-based onboarding

**Key Metrics:** <50ms scene classifier inference | 10-pattern haptic language | WCAG 2.1 AA compliant | Constraint-weighted Dijkstra routing

---

### PROJECT 21: ExperimentIQ - Production-Grade Experimentation & Analytics Platform
**Timeline:** July 2026 - August 2026
**GitHub:** https://github.com/vatsalp2008/ExperimentIQ
**Technologies:** Python, dbt, DuckDB, Snowflake, FastAPI, React 19, TypeScript, PostgreSQL, Apache Kafka, Redis, Statsmodels, SciPy, DoWhy, EconML, MLflow, Recharts, D3.js, Zustand, Tailwind CSS, Docker, GitHub Actions

**Description:**
- End-to-end A/B testing and causal inference platform, SQL-native analytics backbone (Airbnb/Uber/Netflix/Stripe-style)
- Event simulator publishing to Kafka, FastAPI assignment service with consistent-hashing variant assignment
- dbt transformation layer (staging/intermediate/marts) over DuckDB (local) and Snowflake (production-scale)
- Statistical engine: power analysis, Welch's t-test, Mann-Whitney U, CUPED, sequential testing (O'Brien-Fleming), DiD/PSM via DoWhy/EconML
- FastAPI + WebSocket backend, React/TypeScript dashboard (experiment builder, results, diagnostics, causal inference, SQL explorer)
- Optimized the PostgreSQL-backed assignment log query (filtered by experiment ID and timestamp range for the live results panel) by adding a composite index, cutting query time from ~800ms to ~40ms — a 20x improvement confirmed via `EXPLAIN ANALYZE` (sequential scan → index scan)

**Key Metrics:** 10M+ simulated events | 12 dbt models/3 tiers | 45s→0.8s metric computation | 30-45% CUPED variance reduction | 56x query speedup | 20x speedup (800ms→40ms) on assignment log query via composite indexing

---

### PROJECT 22: Open Source Contribution — Spice.ai (spiceai/spiceai)
**Timeline:** July 2026 - Present
**GitHub:** https://github.com/spiceai/spiceai (3k★, Rust, 136 crates, ~1M LOC)
**Status: MERGED.** PR #13507 merged to trunk (commit d8a32b6eb288), on trunk since Aug 26. Two additional PRs open (#13508, #13510), both blocked only on a maintainer click, not anything technical. Always describe as "merged," never "no PRs merged yet."
**Technologies:** Rust, DataFusion (Apache Arrow query engine), Elasticsearch

**Description:**
- Contributing to Spice.ai's open-source Rust query engine (built on Apache DataFusion), a project whose first stated rule is that query results can never be wrong
- **Merged fix (PR #13507):** a silent wrong-results bug where `LIMIT 10` over a partitioned 3-row dataset returned zero rows instead of 3 — a swapped `skip`/`fetch` value still satisfied the existing test, which only asserted the query plan's type rather than its actual output, so the test passed while the query silently returned nothing. Fixed with a one-line change plus 2 regression tests
- **Reported (not implemented) a bug that triggered a major fix by the project's CEO:** filed issue #12747 on a Decimal128 overflow issue; the CEO's own PR #13224 resolved it with a +2,244/−100 line correctness overhaul across 10 files. Must always be described precisely as a bug report that led to the CEO's overhaul, never implied as the user's own implementation
- Found/submitted fixes for a partition-pruning gap (an `OR`-combined predicate across two `partition_by` expressions silently returning the whole table) and two wrapper-forwarding gaps (a trait method with a default implementation that decorators forget to forward)
- Found/submitted 3 smaller fixes: HuggingFace revision mangled into repo name causing 401s (#12430); catalog error double-counted (#12442); contributor-checklist doc fix
- **Root-caused a process deadlock, the standout judgment story:** the user's first three PRs were auto-closed by a stale-bot workflow requiring an `area/` label and an assignee, neither settable from a fork — so the PRs were closed for the inactivity that requirement itself caused. Diagnosed that the stale workflow sets `exempt-draft-pr: true` and that draft status is author-controlled, then reopened the PRs as drafts to keep the work alive until merged
- Filed 4 issues total (#12745, #12746, #12747, #13509); 2 already closed as fixed
- Validated all fixes against the full test suite (2,299 tests passing), `make lint-rust` clean, zero warnings

**Key Metrics:** PR #13507 **merged to trunk** | Reported the bug behind the CEO's own +2,244-line correctness overhaul | 2 additional PRs open, blocked only on maintainer action | 4 issues filed (2 closed as fixed) | Diagnosed and resolved a stale-bot process deadlock via draft-PR status, not just the underlying bugs | 2,299 tests passing, zero lint warnings

**User-provided condensed résumé bullet:** "Contributed to Spice.ai OSS (3k★, Rust, 136 crates, ~1M LOC): diagnosed and fixed a silent data-correctness bug where LIMIT on partitioned datasets returned wrong rows — merged to trunk. A second bug report on decimal overflow drove a 2,200-line correctness fix by the project's CEO."

**User-provided one-liner:** "Merged a data-correctness fix into Spice.ai's Rust query engine and filed the bug report behind a 2,200-line overflow-handling overhaul."

---

### PROJECT 23: Open Source Contribution — Chef Infra Client (chef/chef)
**Timeline:** August 2026 - Present
**Upstream repo:** chef/chef (v19.4.12 at time of work) — configuration management tool, Ruby, Apache 2.0, ~9k GitHub stars, used in production across enterprise fleets
**Fork:** vatsalp2008/chef | **Branch:** fix-integer-umask-13473
**Issue fixed:** chef/chef#13473 ("bash resource breaks on integer umask from chef 16 and up") — opened Jan 2023, open 3 years 7 months before this work
**Pull request:** chef/chef#16260 — "Fix umask property to accept Integer values"
**Status: MERGED.** Merged into chef/chef main on 2026-08-31 as squash commit 4a6992838e, by maintainer jaymzh. Approved by two maintainers (tpowell-progress, jaymzh). Always describe as "merged," never "pending re-approval."

**Description:**
- Triaged **~200 open issues**, evaluated **20 candidates** across two categories (core logic/DSL/node attributes, and resources/providers), verifying each was still reproducible on current main, had no existing PR, and was unit-testable without platform-specific hardware; discarded 2 candidates as unreproducible or already fixed rather than forcing a fix
- Found and fixed a real bug affecting **every Chef resource, not just `bash`**: `Chef::Resource` declared the `umask` property as accepting `[String, Integer]`, but the implementation called `String#oct` unconditionally, which doesn't exist on `Integer`, raising `NoMethodError` for any integer umask before the resource could converge
- Found a second, compounding defect in the same method: the `ensure` block keyed off `umask` instead of `old_value`, so it called `File.umask(nil)` and raised a `TypeError` that silently destroyed the original, more informative error — likely why the issue sat unresolved for 3.5 years, since the resulting stack trace pointed away from the actual bug
- Reproduced the failure in plain Ruby, matching the original reporter's output character-for-character, before touching the repository
- Found the fix through **convention research** rather than inventing a new approach: located the identical idiom already used elsewhere in the codebase (`Chef::FileAccessControl::Unix#mode_from_resource`, `Mixlib::ShellOut#umask=`) and cited both in the PR description — this research proved decisive in review (see below)
- Added **4 RSpec tests** (not 3), each individually verified to fail against stock `main` and pass with the fix applied, including a regression guard proving the `& 0777` bit-mask actually masks rather than passing vacuously
- **Review was genuinely contested.** Maintainer tpowell-progress approved immediately; maintainer jaymzh initially issued CHANGES_REQUESTED, arguing Chef intentionally rejects unquoted integers so users don't silently get unexpected permission values. The objection was withdrawn after another maintainer pointed to the exact in-repo precedent already cited in the PR; jaymzh then requested `& 0777` bit-masking, implemented as a discrete follow-up commit with its own test
- Separately, caught a reviewer's inline suggestion (`unless old_value`) that would have **inverted the guard** and leaked the modified umask into every subsequent resource in the run; verified this broke 5 of 7 `with_umask` specs (including 2 pre-existing ones) before replying with that evidence and applying the reviewer's actual intent correctly (`if old_value`)
- Diagnosed and correctly attributed an unrelated CI flake (1 of 13 jobs failing at `bundle install` before any spec executed, a known upstream hardlink issue) rather than assuming it was caused by the change
- Got CI fully green across all **13 unit-spec jobs** (macOS 14/15, Windows 2022/2025, Ubuntu 22.04/24.04, Rocky 8/9 x86+arm, Debian 11 x86+arm); `rake spec:unit` ran **10,939 examples** with no new failures; lint, func_spec, gem_tests, target_mode_specs, Buildkite verify pipeline, and `cookstyle --chefstyle` all clean
- Shipped in Chef Infra Client `v19.4.22`, with an attributed CHANGELOG entry; 14 subsequent releases with no revert or follow-up correction
- Five additional issues from the same triage shortlist were independently fixed by Chef maintainers within weeks, corroborating the prioritization

**Key Metrics:** PR **merged** into chef/chef main | Shipped in v19.4.22 with an attributed CHANGELOG entry, 14 subsequent releases with no revert | Triaged ~200 issues, evaluated 20 candidates before selecting one | Fixed a bug affecting every Chef resource (not just one), open 3.5 years | Root-caused 2 compounding defects in a single method | Added 4 regression tests | Won a contested code review (CHANGES_REQUESTED → withdrawn) by grounding the fix in existing in-repo precedent | Caught and corrected a second reviewer's suggestion that would have introduced a regression, with verified evidence | CI fully green across 13 platforms + 10,939 unit test examples | 5 additional shortlist issues independently fixed by maintainers within weeks, corroborating the prioritization

---

### PROJECT 24: EchoCoach - Voice-Driven AI Interview Coach
**Timeline:** June 2026 - July 2026 (hackathon, 6 days; eval harness added August 2026)
**GitHub:** https://github.com/vatsalp2008/EchoCoach
**Technologies:** Python, FastAPI, Next.js, OpenAI Whisper, ElevenLabs, Claude API, Cognee (graph-vector memory)

**Description:**
- Built and shipped a full-stack voice-driven AI application solo in a 6-day hackathon (WeMakeDevs × Cognee, Jun 29 - Jul 5, 2026), integrating OpenAI Whisper (speech-to-text), ElevenLabs (text-to-speech), and an LLM into a real-time coaching conversation loop
- Designed a persistent "weakness graph" (via Cognee) so each new session routed toward topics the user had previously struggled with, rather than starting from zero every time
- Designed fallback-first logic and retry handling around every external LLM and voice API call, so a single failed transcription or rate-limited call degraded gracefully instead of breaking the conversation mid-sentence
- Built a from-scratch LLM-as-judge evaluation harness to rigorously compare two system prompt variants (baseline vs. an "always name one specific improvement" variant) across a 25-30 case labeled test set spanning 5 difficulty tiers (strong answers, rambling answers, one-line answers, question-dodging answers, unusual-tone answers)
- Defined a 4-axis rubric (Relevance, Actionability, Tone, Conciseness, each 1-5 scale) with explicit anchor definitions, then used the Claude API as an automated judge to score every response with a numeric score plus a one-sentence justification per axis
- Ran statistical significance testing (t-tests with p-values) rather than just comparing means, distinguishing real effects from noise

**Key Findings from the Eval Harness (real, measured results):**
- **Actionability: +1.3 points (baseline 3.0 → variant A 4.3), p < 0.01** — a statistically significant, real improvement
- **Relevance: +0.1 (3.8 → 3.9), p = 0.41** — not significant, below noise floor
- **Tone: −0.4 (4.1 → 3.7), p = 0.06** — borderline; a real tradeoff worth flagging, not ignoring
- **Conciseness: +0.1 (3.6 → 3.7), p = 0.52** — not significant
- **Cost/latency tradeoff:** input tokens +16% (145→168), output tokens +15% (62→71), latency +9% (780ms→850ms)
- **Overall conclusion:** the "always name one improvement" prompt variant produced a significant, real gain in actionable feedback, but at a real (borderline-significant) cost to tone warmth and a modest (9-16%) increase in token usage and latency — a genuine tradeoff, not a clean win, surfaced through rigorous measurement rather than assumed

**Key Metrics:** 6-day solo hackathon build | Fallback-first reliability design across every external API call | Built eval harness from scratch (no external framework) with labeled test set, defined rubric, LLM-as-judge scoring, and statistical significance testing | Found a statistically significant +1.3 actionability improvement (p<0.01) traded against a borderline tone regression (p=0.06) and 9-16% higher token/latency cost

**Fine-Tuning & Serving Optimization (August 2026):**
- LoRA fine-tuned a small open-source LLM on an expanded training set (built on the original 25-30 eval cases), then ran it through the same eval harness alongside the baseline and prompt-engineered variant
- **Fine-tuning result:** recovered most of the prompt-engineered Actionability gain (4.1 vs. 4.3 for prompting, vs. 3.0 baseline) with far less Tone damage (4.0 vs. 3.7 for prompting, vs. 4.1 baseline) and Conciseness improved too (3.9 vs. 3.6 baseline) — critically, with **zero per-call token overhead**, unlike the prompted variant's 16% token tax. Conclusion: fine-tuning is the better tradeoff at high request volume; prompting may still be preferable at low volume given fine-tuning's upfront setup cost
- Benchmarked serving latency across **fp16, INT8, and INT4** quantization: decode speed was 28 tok/sec (fp16), 24 tok/sec (INT8 — slower despite lower memory use, due to dequantization overhead, a real and non-obvious finding), and 41 tok/sec (INT4 — a genuine 46% speed gain)
- INT4's speed gain came with a quality cost (Actionability 4.1 → 3.8), which sits near the LLM-judge's own noise floor (~0.3-0.5) — reported honestly as a borderline/ambiguous tradeoff rather than overclaiming either a clean win or a clear regression

**Key Metrics (Fine-Tuning & Serving):** LoRA fine-tuning recovered 4.1/4.3 of the prompting Actionability gain with better Tone (4.0 vs 3.7) and zero token overhead | INT8 quantization was slower than fp16 due to dequant overhead (24 vs 28 tok/sec) — a genuine memory-only tradeoff | INT4 delivered a real 46% decode-speed gain (28→41 tok/sec) at a quality cost (4.1→3.8) near the judge's noise floor

---

### PROJECT 25: Afterglow - Real-Time Hand-Gesture Light-Painting Studio
**Timeline:** September 2026 - October 2026
**GitHub:** https://github.com/vatsalp2008/Afterglow
**Technologies:** TypeScript (strict), React 19, Vite, Three.js (WebGL2), Zustand, perfect-freehand, MediaPipe Tasks Vision (HandLandmarker), onnxruntime-web, Yjs + y-websocket, Node.js, SQLite, Cloudflare Workers + Hono, Anthropic Claude API (vision), Python/PyTorch/ONNX, Playwright, Vitest, pnpm + Turborepo

**Description:**
- Built a real-time, in-browser light-painting studio controlled entirely by hand gestures, architected as a pnpm/Turborepo monorepo with a framework-free, deterministic TypeScript core shared across tracking, rendering, and collaboration packages
- Implemented a MediaPipe Tasks `HandLandmarker` pipeline running in a Web Worker and driven by `requestVideoFrameCallback` rather than `requestAnimationFrame`, with every hand frame carrying a monotonic frame ID and capture timestamp so all latency is measured from camera capture rather than inference completion
- Built a signal-processing layer (One Euro, Kalman, and EMA filters behind a common interface) and an explicit gesture state machine (hysteresis-based pinch-to-draw, debounced tool gestures, hand-loss grace periods), validated with replay-based deterministic tests against recorded fixture sessions
- Built a Three.js (WebGL2) renderer with additive-blended neon, ribbon, spark, and ink brushes, bloom post-processing, depth-driven stroke width and brightness from MediaPipe's relative z, and a "darkroom" shader mode with long-exposure fade and fix states
- Added on-device doodle recognition (a small CNN trained on a Quick, Draw! subset, quantized and exported to ONNX, served via onnxruntime-web) alongside $1 Unistroke shape snapping for clean primitive recognition, with undo preserving the original freehand stroke
- Built a "Refine with Claude" feature: strokes render to a PNG and pass through an edge API (Cloudflare Workers + Hono) to a Claude vision model, which returns schema-validated SVG path data that animates back into the canvas as clean vector art
- Implemented real-time multiplayer rooms using Yjs CRDTs with Awareness-based live glowing cursors, per-user scoped undo, and SQLite-backed room persistence with tested offline-edit convergence
- Built a telemetry HUD, a dedicated benchmarking page that replays recorded fixture sessions headlessly to measure filter jitter/lag and gesture precision/recall, and a public "Filter Lab" comparing raw vs. EMA vs. Kalman vs. One Euro signals side by side with live metrics
- Shipped behind a full CI pipeline (lint, typecheck, Vitest unit tests, Playwright end-to-end tests using fixture-injected camera input, bundle-size budget checks, Lighthouse CI) with ADR-documented architecture decisions and a phase-gated build process

**Key Metrics:** Monorepo spanning 3 apps (web, realtime, api) and 5 shared packages (core, tracking, render, collab, ui) | Framework-free, deterministic core package validated with replay-based tests against 8+ recorded fixture sessions | 4 brush types (neon, ribbon, sparks, ink) with bloom, depth-driven brightness, and darkroom long-exposure rendering | On-device ONNX doodle classifier plus $1 Unistroke shape snapping | Claude-vision-powered "Refine" feature converting rough sketches into animated vector art | Real-time multiplayer rooms via Yjs CRDTs with per-user undo and tested offline-merge convergence | Dedicated "Bench" and "Filter Lab" pages backing every performance claim with a reproducible command | Full CI pipeline with bundle-size and Lighthouse budgets enforced

---

### PROJECT 26: PNWER Trade & Tariffs — Cross-Border Trade & Tariff Explorer
**Timeline:** September 2026 - December 2026 (Experience Expo, Northeastern University; Instructor: Dr. Oscar Veliz)
**GitHub:** https://github.com/vatsalp2008/PNWER_Trade-Tariffs
**Live Site:** https://pnwer-trade-tariffs.vercel.app
**Client:** PNWER (Pacific NorthWest Economic Region) — a public-private nonprofit linking 5 U.S. states and 5 Canadian provinces/territories (~27.8M people, $2.2T GDP, ~$3.6B in daily cross-border trade)
**Team:** 6-person team (PM/client liaison, 5 technical leads including Vatsal); Vatsal was a technical lead and the primary builder of the website deliverable
**Technologies:** React, TypeScript, Vite, Recharts, D3.js (maps), Python (pandas data pipeline), GitHub Actions, Vercel

**Description:**
- Designed and built, end to end, a public-data-only web platform helping PNWER assess how U.S.-Canada trade, tariffs, and the USMCA affect its 10 member states and provinces, amid a 2026 USMCA review that moved to annual cadence and new tariffs of up to 50% on steel, aluminum, dairy, alcohol, autos, and (via Canadian counter-tariffs) pulp and paper
- Built an interactive dashboard where a user picks a sector or business and a jurisdiction on a map to see trade volume, jobs, GDP, tariff costs, and an "if USMCA ends" scenario projection
- Built a fact-sheet generation system producing 360 two-page sheets (10 jurisdictions × 36 sectors/businesses), each exportable as a PDF with PNG chart exports, styled to match PNWER's existing "Member Spotlight" format
- Built a Python (pandas) data pipeline pulling official public data only (U.S. Census Bureau, Statistics Canada, BLS, BEA, Bank of Canada, Federal Register), since PNWER has no internal data to draw on
- Built a daily GitHub Actions automation layer that refreshes trade news, opens a GitHub issue for each new U.S. tariff action on Canada, rebuilds trade data when new monthly figures publish, and triggers automatic Vercel redeployment
- Added data-integrity checks that block publishing if totals don't reconcile or Washington's figures don't match the Embassy of Canada's published numbers
- Diagnosed and fixed two failed automation runs caused by Statistics Canada source outages, and redesigned the site's visual identity from green to PNWER's navy/royal blue/sky blue palette at the client's request
- Wrote the team's technical documentation, including a decision log of 28 architecture and design tradeoffs and a setup guide
- Conducted Washington and Idaho research across all industries (trade, tariffs, industry landscape, politics, and PNWER's existing ties), delivered as a 7-page summary and 11-page full report with 14 follow-up questions for the client
- Presented the policy question and project timeline (slides 3-4) at the team's October 29 midterm review

**Key Metrics:** 360 auto-generated two-page fact sheets (10 jurisdictions × 36 sectors/businesses), each with PDF and PNG export | Public-data-only pipeline across 5 official sources (Census, StatCan, BLS, BEA, Bank of Canada) | Daily automated refresh with build-time data validation against a second independent source | 28-decision architecture/tradeoff log | 2 production incidents diagnosed and fixed (Statistics Canada outages) | 4 in-depth jurisdictions + 6 summary-level jurisdictions covering a $2.2T regional economy

---

## PROFESSIONAL EXPERIENCE

### Head Coordinator, Community Space Manager & Audio Manager
**Organization:** BAPS Charities | **Location:** Mumbai, India / Seattle, WA | **Timeline:** Ongoing (long-term, multi-year)

- Coordinated 20+ annual programs serving 50+ participants, managing end-to-end event logistics (venue setup, supply restocking, day-of execution) across diverse age groups
- Grew program attendance by 30% over 2 years by identifying operational gaps through participant feedback and improving scheduling, communication, resource allocation
- Expanded community reach by 40% by leading peer-to-peer outreach campaigns and coordinating volunteer teams
- Serves as Audio Manager, running sound/AV equipment for weekly gatherings

---

### Event Assistant
**Organization:** Northeastern University, Seattle Campus | **Timeline:** May 2026 - Present

- Coordinate logistics for 10+ on-campus events (space setup, venue resets, supply restocking) for student/faculty/prospective-visitor programming
- Deliver campus tours for prospective students and families as a direct point of contact
- Provide operational support across Seattle Campus Operations, including front desk and AV/video conferencing equipment
- Support major university ceremonies including commencement events
- Collaborate with admissions and student services teams

---

### Student Proctor
**Organization:** Northeastern University, College of Professional Studies | **Timeline:** April 2026 - June 2026 (3 months, on-site)

- Proctored 10 cross-campus graduate-level lectures (1 session/week), coordinating between remote and in-person participants across locations
- Supported faculty in maintaining an organized, engaging hybrid learning environment, adapting in real time to technical/logistical issues

---

### Graduate Teaching Assistant - CS 5008 (Data Structures & Algorithms in C)
**Organization:** Khoury College of Computer Sciences, Northeastern University | **Timeline:** January 2026 - May 2026 | **Instructor:** Prof. Tony Mullen

**Note:** Always title this role "Graduate Teaching Assistant" in resumes, not just "Teaching Assistant."

- Conducted weekly office hours and lab sessions for 100+ students in data structures, algorithms, C programming
- Graded 10+ homework and 9+ lab assignments (19+ total) with consistent rubrics
- Debugged complex C implementations (linked lists, trees, graphs, hash tables) with students
- Collaborated with the course instructor to refine assignments and improve learning outcomes

---

### Machine Learning Intern
**Organization:** Kalini Industries | **Timeline:** December 2024 - March 2025

- Predictive classification (TensorFlow, Scikit-learn, 85% accuracy, 5+ algorithms)
- Data pipeline (Pandas, SQL, 20+ features, 15% F1-score improvement)
- Model optimization (40% inference speedup, Flask APIs, production deployment)
- External clients (worked with external clients and customers, talking with them about the products we delivered)

---

### Software Development Intern
**Organization:** Kalini Industries | **Timeline:** January 2024 - April 2024

- Full-stack dashboard (React.js, Flask REST API, 5 production lines, sub-second latency)
- Automation tools (Python, Bash, 8 workflows, 3.2 hours daily savings, Slack API integration)
- Predictive maintenance (Random Forest, Logistic Regression, 85% accuracy, 20% downtime reduction, $50K quarterly savings)
- External clients (worked with external clients and customers, talking with them about the products we delivered)

---

## SUMMARY STATISTICS

### Overview
- **Total Projects:** 26 (24 personal/team projects + 2 open-source contributions, both merged)
- **Total Professional Experience:** 6 positions (BAPS Charities + Event Assistant + Student Proctor + Graduate TA + 2 Kalini internships)
- **Programming Languages:** 15 total — Python, Java, TypeScript, JavaScript, Swift, Kotlin, C++, Rust (2 uses: Seattle Transit + spiceai), C, Go (E-commerce Notification service), Ruby (chef/chef contribution), C# (confirmed, no project tie yet), PowerShell (confirmed, no project tie yet), R, SQL, Shell/Bash

### Chronological Timeline

**2024:** Jan-Apr SDE Intern (Kalini) | Jun-Aug E-commerce Platform + Recommendation | Sep-Dec Housing Price | Dec 2024-Mar 2025 ML Intern (Kalini)

**2025:** Jan-May Investment Prediction | Jun-Oct F1 Analytics | Sep-Oct Seattle Transit | Sep-Nov Chess AI | Nov-Dec AI Agent Discovery | Dec 2025-Jan 2026 StreamLine + NutriTrack

**2026:** Jan-May Graduate TA | Mar-Apr MoodMeals + Traffic Accident | Apr-Jun Student Proctor | Apr-May theManager + FORUM + SWARM | May-Present Event Assistant | Jun-Jul Wardrobe + PulseTrader + ScenarioForge + PerceptNet + TransitLens + EchoCoach | Jul-Aug ExperimentIQ | Jul-Present Open Source (spiceai/spiceai, merged) | Aug Open Source (chef/chef, merged) + EchoCoach fine-tuning/serving work | Sep-Oct Afterglow | Sep-Dec PNWER Trade & Tariffs (Experience Expo)

**Ongoing (multi-year, no fixed start date):** Head Coordinator, BAPS Charities

---

## GITHUB LINKS QUICK REFERENCE

1. StreamLine → https://github.com/vatsalp2008/StreamLine-Distributed-Messaging-Engine
2. E-commerce Platform → https://github.com/vatsalp2008/E-commerce-Platform
3. Intelligent Chess AI → https://github.com/vatsalp2008/Intelligent-Chess-AI
4. AI Agent Discovery → https://github.com/vatsalp2008/AI-Agent-Discovery
5. F1 Analytics Dashboard → https://github.com/vatsalp2008/f1-analytics-dashboard
6. Investment Prediction → https://github.com/vatsalp2008/Investment-Prediction
7. E-commerce Recommendation → https://github.com/vatsalp2008/E-commerce-recommendation-system
8. NutriTrack → https://github.com/vatsalp2008/Food-Tracker
9. Seattle Transit → https://github.com/vatsalp2008/seattle-transit-pathfinder
10. Housing Price → https://github.com/vatsalp2008/Housing-Price-Prediction
11. MoodMeals → https://github.com/vatsalp2008/MoodMeals
12. Traffic Accident Severity → https://github.com/vatsalp2008/Accidents-Severity
13. theManager → https://github.com/aditya-pandita/theManager
14. FORUM → https://github.com/vatsalp2008/Forum
15. SWARM → https://github.com/vatsalp2008/Swarm
16. Wardrobe → https://github.com/vatsalp2008/Wardrobe
17. PulseTrader → https://github.com/vatsalp2008/PulseTrader
18. ScenarioForge → https://github.com/vatsalp2008/ScenarioForge
19. PerceptNet → https://github.com/vatsalp2008/PerceptNet
20. TransitLens → https://github.com/vatsalp2008/TransitLens
21. ExperimentIQ → https://github.com/vatsalp2008/ExperimentIQ
22. Open Source: spiceai/spiceai → https://github.com/spiceai/spiceai (merged, PR #13507)
23. Open Source: chef/chef → https://github.com/chef/chef (fork: https://github.com/vatsalp2008/chef, PR #16260, merged)
24. EchoCoach → https://github.com/vatsalp2008/EchoCoach
25. Afterglow → https://github.com/vatsalp2008/Afterglow
26. PNWER Trade & Tariffs → https://github.com/vatsalp2008/PNWER_Trade-Tariffs (live: https://pnwer-trade-tariffs.vercel.app)

---

## SKILLS & TECHNOLOGIES

### Programming Languages
Python, Java, TypeScript, JavaScript, Swift, Kotlin, C++, C, Rust, Go, Ruby, **C# (new)**, **PowerShell (new)**, R, SQL, Shell/Bash (15 languages)

**Note:** C# and PowerShell are confirmed genuine hands-on skills, not yet tied to a specific master-list project entry. Rust now appears in two places (Seattle Transit + spiceai contribution). Go appears via E-commerce Platform's Notification service. Ruby appears via the chef/chef open-source contribution.

### Frontend Development
React, Next.js, Angular (confirmed real for at least one application — verify current status before reuse), Vue.js (confirmed real for the PathAI application), Django (confirmed real for the PathAI application), GitLab CI (confirmed real for the PathAI application), Vite, Zustand, Three.js, Tailwind CSS, D3.js, Recharts, Material-UI, shadcn/ui, SwiftUI

### Mobile Development (iOS)
Swift 5.9, SwiftUI, UIKit (interactive animations/gesture-driven transitions), Vision Framework, Core ML, Core Data, AVFoundation, Combine, WeatherKit, CryptoKit, PhotosUI, UserNotifications, WebSocket, gRPC

### Backend Development
Spring Boot, Flask, FastAPI, Express, Node.js, REST APIs, GraphQL, WebSocket, gRPC

### Machine Learning & AI
- **Frameworks:** TensorFlow, PyTorch, Scikit-learn, XGBoost, LightGBM, Statsmodels
- **Techniques:** LSTM/GRU, Random Forest, Gradient Boosting, SMOTE, SHAP
- **Causal Inference/Experimentation:** CUPED, Sequential Testing, DoWhy, EconML, DiD, PSM, MLflow
- **LLM Integration:** Claude API, Gemini API, Ollama, LangChain, RAG
- **Agentic AI:** Multi-agent systems, LangGraph, prompt engineering
- **Computer Vision/GPU:** CUDA-accelerated inference (PerceptNet), TensorRT, ONNX
- **Voice AI:** OpenAI Whisper (speech-to-text), ElevenLabs (text-to-speech), Cognee (graph-vector memory)
- **Model Optimization:** LoRA fine-tuning, quantization (fp16/INT8/INT4), LLM-as-judge evaluation harnesses, statistical significance testing (t-tests)
- **Real-Time Computer Vision/Graphics:** MediaPipe Tasks Vision (HandLandmarker), signal filtering (One Euro, Kalman, EMA), gesture state machines, Three.js (WebGL2), onnxruntime-web, Yjs CRDTs for real-time collaboration

### Databases & Data Warehousing
PostgreSQL, MongoDB, MySQL, Supabase, Redis, DuckDB, Snowflake, BigQuery, SQLite, Drizzle ORM, dbt, Cosmos DB, DynamoDB

**Database framing:** Projects built with PostgreSQL (e.g., E-Commerce Platform, ExperimentIQ, Seattle Transit, Housing Price Prediction) have also genuinely been implemented with MySQL, and projects built with DynamoDB (e.g., Investment Prediction Platform's NoSQL layer) have also genuinely been implemented with MongoDB. When tailoring a resume, match the database framing to whichever specific SQL or NoSQL product the job description names.

### Distributed Systems
Microservices, Kafka, Event-Driven Architecture, Saga Pattern, Docker, Kubernetes

### Cloud & DevOps
AWS, Google Cloud (including BigQuery), Azure (Cosmos DB), Vercel, Fly.io, Docker, Kubernetes, Terraform, CI/CD, GitHub Actions

### Data Science & Visualization
Pandas, NumPy, Apache Spark, Matplotlib, Seaborn, Folium, Arcade, Tableau, Power BI, D3.js (geographic/choropleth mapping), Recharts

### Public Data & Policy Analytics
U.S. Census Bureau, Statistics Canada, BLS, BEA, Bank of Canada, Federal Register — public-data-only pipelines for trade/tariff/economic policy analysis (PNWER)

### Payment & Business Integrations
Stripe API (SWARM), QuickBooks API (theManager)

### Tools & Platforms
Git, GitHub, VS Code, Jupyter, Postman, Figma, Tauri, Playwright, Browserbase

### Open Source
Merged contribution to spiceai/spiceai (Rust, Apache DataFusion query engine) — PR #13507 merged to trunk, plus a bug report that drove the CEO's own 2,244-line correctness overhaul
Merged contribution to chef/chef (Ruby, Chef Infra Client) — PR #16260 merged to main, 2026-08-31

---

## CAREER POSITIONING

### Big Tech
Google, Meta, Amazon, Microsoft, Apple — Full-stack + ML + distributed systems + scale + native iOS

### Autonomous Vehicle Companies
Waymo, Cruise, Aurora, Motional, Mobileye, Tesla, Lucid — PerceptNet (now with CUDA) + ScenarioForge

### Financial Technology
Bloomberg, Citadel, Two Sigma, Jane Street, Robinhood, Stripe, Coinbase — PulseTrader (now with BigQuery)

### Data & Experimentation Platforms
Airbnb, Uber, Netflix, Stripe, Meta, Booking.com, Spotify — ExperimentIQ

### AI Companies
Anthropic, OpenAI, Cohere, LangChain, Perplexity — 6 agentic AI projects, plus EchoCoach's voice AI and eval/fine-tuning work

### Distributed Data Systems / Infra
Databricks, Snowflake, ClickHouse, Spice.ai, database/query-engine companies — merged open-source contribution fixing real correctness bugs in a Rust/DataFusion query engine, plus a bug report that drove the CEO's own overhaul

### Infrastructure & Configuration Management
Chef/Progress Software, HashiCorp, Puppet, Ansible, Red Hat — merged open-source contribution to Chef Infra Client fixing a bug affecting every Chef resource, with a contested code review won on the strength of in-repo precedent research

### Mobile-First Companies
Instagram, Snap, TikTok, Pinterest, Uber, DoorDash, Lyft — iOS (now with UIKit) + Android

### Accessibility & Social Impact
Google Accessibility, Microsoft Accessibility, Be My Eyes, Aira, Hopelink — TransitLens

### Civic Tech & Nonprofits
Code for America, Civic Hall, Knight Foundation, Urban Institute — FORUM, TransitLens, Traffic Accident, PNWER Trade & Tariffs (public-private trade policy nonprofit)

### Economic Policy & Trade Analytics
PNWER, think tanks, government economic offices, trade associations — PNWER Trade & Tariffs: a public-data-only trade/tariff dashboard and auto-generated fact-sheet system built for a 10-jurisdiction cross-border economic region

### Robotics & Simulation
Boston Dynamics, Applied Intuition, Foretellix, NVIDIA Isaac Sim, Cognata — ScenarioForge + PerceptNet (CUDA)

### Creative Tools & Real-Time Graphics
Figma, Adobe, Unity, NVIDIA (graphics/creative tooling), WebGL/real-time rendering companies — Afterglow (MediaPipe hand tracking, Three.js/WebGL2 rendering, real-time multiplayer via Yjs CRDTs, on-device ONNX inference)

### Startups
YC Companies, Series A-C — SWARM (now with real Stripe billing integration)

### Research Institutions
PhD Programs, Think Tanks, Academic Labs — FORUM, EchoCoach's rigorous eval methodology

### Government & Civic Tech
USDS, 18F, Municipal Innovation — FORUM + Traffic Accident + PNWER Trade & Tariffs

### Founding Your Own Company
VC-Backable, Bootstrapped, Grant-Funded — SWARM (real Stripe integration), phased roadmap

---

## PORTFOLIO HIGHLIGHTS

### Technical Mastery
✅ 15 languages (incl. confirmed C#, PowerShell) ✅ Full-stack ✅ ML/DL (5 projects) ✅ AI/LLM (7 agentic/voice projects) ✅ Distributed systems (incl. Kubernetes) ✅ Security/compliance ✅ Causal inference (CUPED, DiD, PSM) ✅ Systems-level correctness rigor (2 merged OSS contributions fixing real bugs, in Rust and Ruby codebases) ✅ Business/payment integrations (Stripe, QuickBooks) ✅ Model optimization (LoRA fine-tuning, quantization, rigorous evaluation harnesses) ✅ Real-time computer vision & graphics engineering (hand tracking, signal filtering, WebGL rendering, CRDT-based multiplayer) ✅ Public-data pipeline engineering with build-time data validation and automated multi-source reconciliation (PNWER)

### Domain Breadth
✅ E-commerce ✅ Sports analytics (Power BI) ✅ FinTech (PulseTrader, BigQuery) ✅ Autonomous Vehicles ✅ Healthcare/wellness ✅ Fashion/Style ✅ Public safety (Tableau) ✅ Accessibility ✅ Civic tech ✅ Developer tools ✅ Legal/compliance ✅ Experimentation/data infra ✅ Open-source distributed data systems (spiceai/spiceai, merged) ✅ Open-source infrastructure/config management (chef/chef, merged) ✅ Voice AI / interview coaching (EchoCoach) ✅ Creative tools / real-time computer vision (Afterglow) ✅ Trade & economic policy analytics (PNWER)

### Standout Qualities
✅ Research capability ✅ Entrepreneurial execution ✅ Production mindset ✅ Ethical awareness ✅ Open source commitment (2 merged contributions: spiceai/spiceai, chef/chef) ✅ Scale handling (7.7M records, 10M+ simulated events) ✅ Community leadership (BAPS Charities: 20+ programs, 50+ participants, 30% attendance growth, 40% reach expansion) ✅ Rigorous, honest measurement (EchoCoach's eval harness surfaced a real tradeoff rather than a clean win) ✅ Systems engineering discipline under a phase-gated process (Afterglow: pure deterministic core, replay-based testing, documented ADRs, CI-enforced performance budgets) ✅ Real-world client delivery (PNWER: direct nonprofit client, 6-person team, live production site with daily automation and documented decision-making)

---

**Last Updated:** November 2026
**Document Version:** v23.0 (supersedes v21.0 and the v22.0 update layer)
**Total Projects:** 26 | **Total Professional Experience:** 6
**Contact:** https://github.com/vatsalp2008
**Portfolio:** https://vatsalp2008.github.io/Portfolio/

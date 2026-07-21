import json
import os
import shutil
import sqlite3

from flask import Blueprint, render_template, jsonify, request

from models.db import db
import config

history_bp = Blueprint('history', __name__)


def _parse_test_result(result: dict) -> dict:
    """Parse JSON fields in test result"""
    test = dict(result)
    if test.get('details'):
        try:
            test['details'] = json.loads(test['details'])
        except (json.JSONDecodeError, TypeError):
            pass
    if test.get('expected'):
        try:
            test['expected'] = json.loads(test['expected'])
        except (json.JSONDecodeError, TypeError):
            pass
    return test


@history_bp.route('/history')
def history():
    """Evaluation history page with pagination"""
    page = request.args.get('page', 1, type=int)
    per_page = 20

    offset = (page - 1) * per_page
    runs = db.get_all_runs(limit=per_page, offset=offset)
    total_count = db.get_runs_count()
    incomplete_count = db.get_incomplete_count()
    total_pages = (total_count + per_page - 1) // per_page  # Ceiling division

    return render_template('history.html',
                           runs=runs,
                           page=page,
                           total_pages=total_pages,
                           total_count=total_count,
                           incomplete_count=incomplete_count)


@history_bp.route('/api/history/<int:run_id>', methods=['DELETE'])
def api_delete_run(run_id):
    """Delete an evaluation run and all related data"""
    try:
        success = db.delete_run(run_id)
        if success:
            log_dir = os.path.join(config.BASE_DIR, 'logs', str(run_id))
            if os.path.isdir(log_dir):
                shutil.rmtree(log_dir)
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@history_bp.route('/api/history/clear', methods=['POST'])
def api_clear_history():
    """Delete all evaluation runs and related data"""
    try:
        count = db.clear_all_runs()
        # Also remove all log directories
        log_dir = os.path.join(config.BASE_DIR, 'logs')
        if os.path.isdir(log_dir):
            shutil.rmtree(log_dir)
            os.makedirs(log_dir, exist_ok=True)
        return jsonify({'success': True, 'deleted': count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@history_bp.route('/api/history/clear-incomplete', methods=['POST'])
def api_clear_incomplete():
    """Delete all incomplete evaluation runs (completed_at IS NULL) and related data"""
    try:
        count = db.clear_incomplete_runs()
        # Clean up orphan log directories (runs no longer in DB)
        log_dir = os.path.join(config.BASE_DIR, 'logs')
        if os.path.isdir(log_dir):
            for entry in os.listdir(log_dir):
                entry_path = os.path.join(log_dir, entry)
                if os.path.isdir(entry_path):
                    try:
                        run_id = int(entry)
                        if not db.get_evaluation_run(run_id):
                            shutil.rmtree(entry_path)
                    except (ValueError, OSError):
                        pass
        return jsonify({'success': True, 'deleted': count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@history_bp.route('/history/<int:run_id>')
def history_detail(run_id):
    """Evaluation detail page - frozen result view"""
    run_info = db.get_evaluation_run(run_id)
    test_results = db.get_test_results(run_id)
    stats = db.get_run_stats(run_id)

    if not run_info:
        return "Run not found", 404

    # Individual test counts (from individual_test_results)
    with db._connect() as _conn:
        _c = _conn.cursor()
        _c.execute("SELECT COUNT(*), SUM(status='passed') FROM individual_test_results WHERE run_id=?", (run_id,))
        _row = _c.fetchone()
    individual_stats = {'total': _row[0] or 0, 'passed': _row[1] or 0}

    # Extract unique domains from actual test results (preserving order)
    from evaluator.test_manager import test_manager
    all_domain_meta = {d['id']: d.get('name', d['id']) for d in test_manager.list_domains(include_disabled=True)}
    seen = set()
    domains = []
    for r in test_results:
        d = r.get('domain')
        if d and d not in seen:
            seen.add(d)
            domains.append(d)

    return render_template('history_detail.html',
                           run_info=run_info,
                           test_results=test_results,
                           stats=stats,
                           individual_stats=individual_stats,
                           domains=domains,
                           domain_names=all_domain_meta)


@history_bp.route('/api/run/<int:run_id>')
def api_run_details(run_id):
    """Get details for a specific run"""
    run_info = db.get_evaluation_run(run_id)
    test_results = db.get_test_results(run_id)
    stats = db.get_run_stats(run_id)

    return jsonify({
        'run_info': run_info,
        'test_results': test_results,
        'stats': stats
    })


@history_bp.route('/api/run/<int:run_id>/notes', methods=['PATCH'])
def api_update_run_notes(run_id):
    """Update notes for a specific evaluation run"""
    data = request.get_json()
    if data is None or 'notes' not in data:
        return jsonify({'error': 'Missing notes field'}), 400
    notes = data['notes'].strip() if data['notes'] else ''
    success = db.update_run_notes(run_id, notes)
    if not success:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify({'success': True})


@history_bp.route('/api/run/<int:run_id>/summary', methods=['PATCH'])
def api_update_run_summary(run_id):
    """Update summary for a specific evaluation run"""
    data = request.get_json()
    if data is None or 'summary' not in data:
        return jsonify({'error': 'Missing summary field'}), 400
    summary = data['summary'].strip() if data['summary'] else ''
    success = db.update_run_summary(run_id, summary)
    if not success:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify({'success': True})


@history_bp.route('/api/run/<int:run_id>/matrix')
def api_run_matrix(run_id):
    """Get test matrix for a specific run (same format as /api/evaluator/test_matrix)"""
    test_results = db.get_test_results(run_id)
    run_info = db.get_evaluation_run(run_id)
    model_name = run_info.get("model_name") if run_info else None

    # Get unique domains from actual test results
    domains_in_run = set(r["domain"] for r in test_results)

    # Organize by domain and level
    matrix = {}
    for domain in domains_in_run:
        matrix[domain] = {}
        for level in range(1, 6):
            matrix[domain][level] = {
                "status": "pending",
                "score": None,
                "details": None,
                "prompt": None,
                "response": None,
                "expected": None,
                "duration_ms": None,
                "model_name": None
            }

    # Fill with actual results
    for result in test_results:
        domain = result["domain"]
        level = result["level"]

        if domain in matrix and level in matrix[domain]:
            matrix[domain][level] = {
                "status": result["status"],
                "score": result["score"],
                "details": json.loads(result["details"]) if result["details"] else None,
                "prompt": result.get("prompt"),
                "response": result.get("response"),
                "expected": json.loads(result["expected"]) if result.get("expected") else None,
                "duration_ms": result.get("duration_ms"),
                "model_name": result.get("model_name")
            }

    return jsonify({
        "domains": matrix,
        "run_id": run_id,
        "model_name": model_name,
        "status": "completed"
    })


@history_bp.route('/api/run/<int:run_id>/tests/<domain>/<int:level>')
def api_run_cell_tests(run_id, domain, level):
    """Get individual test results for a specific cell (domain + level)"""
    individual_results = db.get_individual_test_results(run_id, domain, level)

    # Parse JSON fields
    tests = []
    for result in individual_results:
        test = dict(result)

    # Parse JSON fields
        if test.get('details'):
            try:
                test['details'] = json.loads(test['details'])
            except (json.JSONDecodeError, TypeError):
                pass
        if test.get('expected'):
            try:
                test['expected'] = json.loads(test['expected'])
            except (json.JSONDecodeError, TypeError):
                pass
        tests.append(test)

    return jsonify({
        "run_id": run_id,
        "domain": domain,
        "level": level,
        "tests": tests
    })


# ============================================
# V1 History API Endpoints
# ============================================

@history_bp.route('/api/v1/history/last/id')
def api_v1_history_last_id():
    """Get the most recent run ID and info"""
    last_run = db.get_last_run()
    if not last_run:
        return jsonify({"error": "No evaluation runs found"}), 404

    return jsonify({
        "run_id": last_run.get("run_id"),
        "model_name": last_run.get("model_name"),
        "started_at": last_run.get("started_at"),
        "completed_at": last_run.get("completed_at"),
        "status": last_run.get("status")
    })


@history_bp.route('/api/v1/history/last/<domain>/<int:level>')
def api_v1_history_last_domain_level(domain, level):
    """Get test results for the most recent run at specified domain/level"""
    last_run_id = db.get_last_run_id()
    if not last_run_id:
        return jsonify({"error": "No evaluation runs found"}), 404

    individual_results = db.get_individual_test_results(last_run_id, domain, level)

    if not individual_results:
        return jsonify({
            "run_id": last_run_id,
            "domain": domain,
            "level": level,
            "tests": [],
            "message": "No test results found for this domain/level"
        })

    tests = [_parse_test_result(r) for r in individual_results]

    return jsonify({
        "run_id": last_run_id,
        "domain": domain,
        "level": level,
        "tests": tests
    })


@history_bp.route('/api/v1/history/<int:run_id>/<domain>/<int:level>')
def api_v1_history_run_domain_level(run_id, domain, level):
    """Get test results for a specific run/domain/level with full details"""
    # Verify run exists
    run_info = db.get_evaluation_run(run_id)
    if not run_info:
        return jsonify({"error": f"Run '{run_id}' not found"}), 404

    individual_results = db.get_individual_test_results(run_id, domain, level)

    if not individual_results:
        return jsonify({
            "run_id": run_id,
            "domain": domain,
            "level": level,
            "tests": [],
            "message": "No test results found for this domain/level"
        })

    tests = [_parse_test_result(r) for r in individual_results]

    return jsonify({
        "run_id": run_id,
        "domain": domain,
        "level": level,
        "model_name": run_info.get("model_name"),
        "tests": tests
    })


@history_bp.route('/api/v1/result/<int:result_id>')
def api_v1_result(result_id):
    """Get a single individual test result by its primary key (full details, no truncation)"""
    row = db.get_individual_test_result_by_id(result_id)
    if not row:
        return jsonify({"error": f"Result {result_id} not found"}), 404
    return jsonify(_parse_test_result(row))

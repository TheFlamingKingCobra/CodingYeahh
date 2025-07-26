from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import threading
import time

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Load prompts from file
def load_prompts(file_path='prompts.txt'):
    try:
        with open(file_path, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return ["Default prompt: describe your favorite day."]

PROMPTS = load_prompts()

games = {}
timers = {}
players_in_room = {}  # track players in each room
MAX_ROUNDS = 5

# Helper Functions
def generate_ai_answer(user_id, prompt):
    # Simulated AI answer (reversed prompt for now)
    return f"AI response for {user_id}: {prompt[::-1]}"

def create_new_game(room_id):
    games[room_id] = {
        "round": 1,
        "max_rounds": MAX_ROUNDS,
        "prompts": PROMPTS.copy(),
        "current_prompt": None,
        "real_answers": {},
        "ai_answers": {},
        "guesses": {},
        "scores": {},  # simple score
        "stats": {}    # detailed per-player stats
    }

# Timer Thread
class StoppableTimerThread(threading.Thread):
    def __init__(self, room_id, duration, phase):
        super().__init__(daemon=True)
        self.room_id = room_id
        self.duration = duration
        self.phase = phase
        self._stop_event = threading.Event()

    def run(self):
        end = time.time() + self.duration
        timers[self.room_id] = {'end_time': end, 'phase': self.phase, 'thread': self}

        while time.time() < end and not self._stop_event.is_set():
            socketio.emit(
                'timer_update',
                {'time_left': int(end - time.time()), 'phase': self.phase},
                room=self.room_id
            )
            time.sleep(1)

        if not self._stop_event.is_set():
            socketio.emit('phase_ended', {'phase': self.phase}, room=self.room_id)
            timers.pop(self.room_id, None)

    def stop(self):
        self._stop_event.set()
        timers.pop(self.room_id, None)

def start_phase_timer(room_id, phase, duration):
    if room_id in timers:
        return
    t = StoppableTimerThread(room_id, duration, phase)
    t.start()
    timers[room_id] = {'thread': t}

def end_phase_early(room_id, phase):
    if room_id in timers:
        timers[room_id]['thread'].stop()
        socketio.emit('phase_ended', {'phase': phase}, room=room_id)

# Socket Events
@socketio.on('join_room')
def on_join(data):
    room = data.get('room_id')
    user = data.get('user_id')

    if room and user:
        join_room(room)
        players_in_room.setdefault(room, set()).add(user)
        emit('status', {'msg': f'{user} has joined {room}.'}, room=room)

@socketio.on('leave_room')
def on_leave(data):
    room = data.get('room_id')
    user = data.get('user_id')

    if room and user:
        leave_room(room)
        if room in players_in_room:
            players_in_room[room].discard(user)
        emit('status', {'msg': f'{user} has left {room}.'}, room=room)

# Game Endpoints
@app.route('/game/start', methods=['POST'])
def start_game():
    room = request.json.get('room_id')
    if not room:
        return jsonify({"error": "room_id is required"}), 400

    create_new_game(room)
    start_phase_timer(room, 'answer_submission', 30)
    return jsonify({"message": f"Game commenced in {room}", "max_rounds": MAX_ROUNDS})

@app.route('/game/next_round', methods=['GET'])
def next_round():
    room = request.args.get('room_id')
    if room not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    g = games[room]
    if g["round"] > g["max_rounds"]:
        return jsonify({"message": "Game Over", "scores": g["scores"]})

    if not g["current_prompt"]:
        if not g["prompts"]:
            g["prompts"] = PROMPTS.copy()
        g["current_prompt"] = random.choice(g["prompts"])
        g["prompts"].remove(g["current_prompt"])

    return jsonify({
        "round": g["round"],
        "prompt": g["current_prompt"],
        "max_rounds": g["max_rounds"]
    })

@app.route('/game/submit_answer', methods=['POST'])
def submit_answer():
    data = request.json
    room = data.get('room_id')
    user = data.get('user_id')
    answer = data.get('answer')

    if not all([room, user, answer]):
        return jsonify({"error": "room_id, user_id, and answer are required"}), 400
    if room not in games:
        return jsonify({"error": "Invalid room_id"}), 400
    if len(answer.strip().split()) < 5:
        return jsonify({"error": "Response must contain at least five words"}), 400

    if room not in timers or timers[room]['phase'] != 'answer_submission':
        return jsonify({"error": "The answer submission phase has ended"}), 400
    if time.time() > timers[room]['end_time']:
        return jsonify({"error": "The answer submission time has elapsed"}), 400

    g = games[room]
    prompt = g['current_prompt']
    if not prompt:
        return jsonify({"error": "No prompt set"}), 400

    g['real_answers'][user] = answer
    g['ai_answers'][user] = generate_ai_answer(user, prompt)

    g['scores'].setdefault(user, 0)
    g['stats'].setdefault(user, {"correct": 0, "wrong": 0, "streak": 0, "max_streak": 0})

    expected_players = players_in_room.get(room, set())
    if expected_players.issubset(set(g['real_answers'].keys())):
        end_phase_early(room, 'answer_submission')

    return jsonify({
        "message": f"Response saved for {user}",
        "ai_response": g['ai_answers'][user]
    })

@app.route('/game/get_answers', methods=['GET'])
def get_answers():
    room = request.args.get('room_id')
    if room not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    g = games[room]
    answers = [{"id": f"{u}_real", "text": r} for u, r in g['real_answers'].items()] + \
              [{"id": f"{u}_ai", "text": a} for u, a in g['ai_answers'].items()]
    random.shuffle(answers)
    return jsonify({"answers": answers})

@app.route('/game/submit_guess', methods=['POST'])
def submit_guess():
    d = request.json
    room = d.get('room_id')
    guesser = d.get('guessing_user')
    gid = d.get('guessed_id')
    gtype = d.get('guess_type')

    if not all([room, guesser, gid, gtype]):
        return jsonify({"error": "Missing fields"}), 400
    if room not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    g = games[room]
    g['guesses'].setdefault(guesser, {})[gid] = gtype
    g['stats'].setdefault(guesser, {"correct": 0, "wrong": 0, "streak": 0, "max_streak": 0})

    correct_type = 'AI' if gid.endswith('_ai') else 'human'
    correct = gtype.lower() == correct_type.lower()

    if correct:
        g['scores'][guesser] = g['scores'].get(guesser, 0) + 1
        g['stats'][guesser]["correct"] += 1
        g['stats'][guesser]["streak"] += 1
        g['stats'][guesser]["max_streak"] = max(
            g['stats'][guesser]["max_streak"], g['stats'][guesser]["streak"]
        )
    else:
        g['stats'][guesser]["wrong"] += 1
        g['stats'][guesser]["streak"] = 0

    return jsonify({
        "message": "Guess recorded",
        "correct": correct,
        "scores": g['scores']
    })

@app.route('/game/advance_round', methods=['POST'])
def advance_round():
    room = request.json.get('room_id')
    if room not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    g = games[room]
    if g["round"] >= g["max_rounds"]:
        return jsonify({"message": "Maximum rounds reached", "scores": g['scores']})

    g.update({
        "round": g["round"] + 1,
        "current_prompt": None,
        "real_answers": {},
        "ai_answers": {},
        "guesses": {}
    })

    start_phase_timer(room, 'answer_submission', 30)
    return jsonify({"message": f"Advanced to round {g['round']}"})

@app.route('/game/final_summary', methods=['GET'])
def final_summary():
    room = request.args.get('room_id')
    if room not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    g = games[room]
    scores = g['scores']
    stats = g['stats']

    if not scores:
        return jsonify({"message": "No scores yet"})

    max_s = max(scores.values())
    min_s = min(scores.values())

    leaderboard = sorted(scores.items(), key=lambda x: -x[1])

    top_detectives = [p for p, s in scores.items() if s == max_s]
    most_fooled = [p for p, s in scores.items() if s == min_s]

    awards = {
        "Top Detective": {
            "players": top_detectives,
            "max_streak": max(stats.get(p, {}).get("max_streak", 0) for p in top_detectives)
        },
        "Most Fooled by AI": {
            "players": most_fooled,
            "wrong_guesses": {p: stats.get(p, {}).get("wrong", 0) for p in most_fooled}
        }
    }

    return jsonify({"leaderboard": leaderboard, "awards": awards})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
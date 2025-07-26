from flask import Flask, request, jsonify  
from flask_socketio import SocketIO, emit, join_room  
import random  
import threading  
import time  

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # Allows frontend connections by enabling CORS

def load_prompts(file_path='prompts.txt'):
    with open(file_path, 'r') as f:
        return [line.strip() for line in f if line.strip()]  

PROMPTS = load_prompts()  

games = {}  # Dictionary to hold game states per room_id
MAX_ROUNDS = 5  

timers = {}  # Store timer info per game room

def generate_ai_answer(user_id, prompt):
    # Placeholder AI answer generation: reverses prompt string
    return f"AI answer for {user_id}: {prompt[::-1]}"

def create_new_game(room_id):
    # Initialize or reset the game state for a room
    games[room_id] = {
        "round": 1,
        "max_rounds": MAX_ROUNDS,
        "prompts": PROMPTS.copy(),
        "current_prompt": None,
        "real_answers": {},  
        "ai_answers": {},    
        "guesses": {},     
        "scores": {},         
    }

def timer_thread(room_id, phase_duration, phase_name):
    end_time = time.time() + phase_duration
    timers[room_id] = {'end_time': end_time, 'phase': phase_name}

    while True:
        time_left = end_time - time.time()
        if time_left <= 0:
            break
        # Emit remaining time to all clients in the room
        socketio.emit('timer_update', {'time_left': int(time_left), 'phase': phase_name}, room=room_id)
        time.sleep(1)

    # Notify clients that this phase has ended
    socketio.emit('phase_ended', {'phase': phase_name}, room=room_id)
    # Remove timer info after completion
    timers.pop(room_id, None)

def start_phase_timer(room_id, phase_name, duration_seconds):
    """
    Starts a new timer thread for a specific phase in a room.
    """
    if room_id in timers:
        pass

    thread = threading.Thread(target=timer_thread, args=(room_id, duration_seconds, phase_name))
    thread.daemon = True  # Daemon thread ends when main program exits
    thread.start()
    timers[room_id]['thread'] = thread

@socketio.on('join_room')
def on_join(data):
    """
    WebSocket event: client requests to join a room.
    Adds client to SocketIO room and notifies others.
    """
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if not room_id or not user_id:
        return  # Ignore invalid requests

    join_room(room_id)
    emit('status', {'msg': f'{user_id} has joined room {room_id}.'}, room=room_id)

@app.route('/game/start', methods=['POST'])
def start_game():
    """
    HTTP endpoint to start a new game in the specified room.
    Initializes game state and starts the first answer submission timer.
    """
    data = request.json
    room_id = data.get('room_id')
    if not room_id:
        return jsonify({"error": "room_id is required"}), 400

    create_new_game(room_id)

    # Start timer for the answer submission phase (30 seconds for our game)
    start_phase_timer(room_id, 'answer_submission', 30)

    return jsonify({
        "message": f"Game started in room {room_id}",
        "max_rounds": MAX_ROUNDS
    })

@app.route('/game/next_round', methods=['GET'])
def next_round():
    """
    HTTP endpoint to get info about the current round and prompt.
    Returns final scores if the game is over.
    """
    room_id = request.args.get('room_id')
    if not room_id or room_id not in games:
        return jsonify({"error": "Invalid or missing room_id"}), 400

    game_state = games[room_id]

    # Check if the game has ended
    if game_state["round"] > game_state["max_rounds"]:
        return jsonify({
            "message": "Game Over",
            "scores": game_state["scores"]
        })

    # Pick a new prompt if not set for this round
    if not game_state["current_prompt"]:
        if len(game_state["prompts"]) == 0:
            game_state["prompts"] = PROMPTS.copy()
        prompt = random.choice(game_state["prompts"])
        game_state["prompts"].remove(prompt)
        game_state["current_prompt"] = prompt
    else:
        prompt = game_state["current_prompt"]

    return jsonify({
        "round": game_state["round"],
        "prompt": prompt,
        "max_rounds": game_state["max_rounds"]
    })

@app.route('/game/submit_answer', methods=['POST'])
def submit_answer():
    """
    Players submit their real answers to the current prompt.
    Enforces minimum 5-word limit.
    AI answers are generated automatically here.
    """
    data = request.json
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    answer = data.get('answer')

    if not all([room_id, user_id, answer]):
        return jsonify({"error": "room_id, user_id, and answer are required"}), 400
    if room_id not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    # Minimum 5-word answer rule to give AI more stuff to work off
    word_count = len(answer.strip().split())
    if word_count < 5:
        return jsonify({"error": "Answer must contain at least 5 words"}), 400

    game_state = games[room_id]
    prompt = game_state['current_prompt']
    if not prompt:
        return jsonify({"error": "No prompt set for current round"}), 400

    # Save the real answer
    game_state['real_answers'][user_id] = answer

    # Generate and save AI answer for this user and prompt
    ai_answer = generate_ai_answer(user_id, prompt)
    game_state['ai_answers'][user_id] = ai_answer

    # Initialize user score if not present
    if user_id not in game_state['scores']:
        game_state['scores'][user_id] = 0

    return jsonify({
        "message": f"Answers saved for user {user_id} in room {room_id}",
        "ai_answer": ai_answer
    })

@app.route('/game/get_answers', methods=['GET'])
def get_answers():
    """
    Returns all answers (both real and AI), shuffled and anonymized.
    Used for guessing/voting.
    """
    room_id = request.args.get('room_id')
    if not room_id or room_id not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    game_state = games[room_id]

    answers = []
    for user, real in game_state['real_answers'].items():
        answers.append({"id": f"{user}_real", "text": real})
    for user, ai in game_state['ai_answers'].items():
        answers.append({"id": f"{user}_ai", "text": ai})

    random.shuffle(answers)  # Shuffle answers so players don’t know which answer is human/AI

    return jsonify({"answers": answers})

@app.route('/game/submit_guess', methods=['POST'])
def submit_guess():
    """
    Players submit guesses on whether an answer is AI or human.
    Scores are updated if guess is correct.
    """
    data = request.json
    room_id = data.get('room_id')
    guessing_user = data.get('guessing_user')  # Player making the guess
    guessed_id = data.get('guessed_id')        # The answer ID guessed (e.g."user1_ai")
    guess_type = data.get('guess_type')        # AI or human (guess)

    if not all([room_id, guessing_user, guessed_id, guess_type]):
        return jsonify({"error": "room_id, guessing_user, guessed_id, and guess_type required"}), 400
    if room_id not in games:
        return jsonify({"error": "Invalid room_id"}), 400

    game_state = games[room_id]

    # Store the guess for this user and answer
    game_state['guesses'].setdefault(guessing_user, {})[guessed_id] = guess_type

    # Determine the correct answer type by the ID suffix
    correct_type = 'AI' if guessed_id.endswith('_ai') else 'human'

    # Give point (+1) if guess matches the correct type
    if guess_type.lower() == correct_type.lower():
        game_state['scores'][guessing_user] = game_state['scores'].get(guessing_user, 0) + 1

    return jsonify({
        "message": f"Guess recorded for {guessing_user}",
        "correct": guess_type.lower() == correct_type.lower(),
        "scores": game_state['scores']
    })

@app.route('/game/advance_round', methods=['POST'])
def advance_round():
    """
    Advances the game to the next round, resets round data,
    and starts a new answer submission timer.
    """
    data = request.json
    room_id = data.get('room_id')
    if not room_id or room_id not in games:
        return jsonify({"error": "Invalid or missing room_id"}), 400

    game_state = games[room_id]

    # If max rounds reached (5), return final scores
    if game_state["round"] >= game_state["max_rounds"]:
        return jsonify({
            "message": "Max rounds reached",
            "scores": game_state['scores']
        })

    # Increment round number and reset round-specific data
    game_state['round'] += 1
    game_state['current_prompt'] = None
    game_state['real_answers'] = {}
    game_state['ai_answers'] = {}
    game_state['guesses'] = {}

    # Start new answer submission timer for next round
    start_phase_timer(room_id, 'answer_submission', 30)

    return jsonify({
        "message": f"Advanced to round {game_state['round']} in room {room_id}"
    })

if __name__ == '__main__':
    # Run the Flask-SocketIO server
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)    
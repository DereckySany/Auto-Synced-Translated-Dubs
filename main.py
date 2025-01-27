#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

# Project Title: Auto Synced Translated Dubs (https://github.com/ThioJoe/Auto-Synced-Translated-Dubs)
# Author / Project Owner: "ThioJoe" (https://github.com/ThioJoe)
# License: GPLv3
# NOTE: By contributing to this project, you agree to the terms of the GPLv3 license, and agree to grant the project owner the right to also provide or sell this software, including your contribution, to anyone under any other license, with no compensation to you.

version = '0.7.0'
print(f"------- 'Auto Synced Translated Dubs' script by ThioJoe - Release version {version} -------")

# Import other files
import TTS
import audio_builder
import auth
from utils import parseBool
# Import built in modules
import re
import configparser
import os
import pathlib
import copy
# Import other modules
import ffprobe
import langcodes
from operator import itemgetter

# EXTERNAL REQUIREMENTS:
# rubberband binaries: https://breakfastquay.com/rubberband/ - Put rubberband.exe and sndfile.dll in the same folder as this script
# ffmpeg installed: https://ffmpeg.org/download.html


# ====================================== SET CONFIGS ================================================
# MOVE THIS INTO A DICTIONARY VARIABLE AT SOME POINT
outputFolder = "output"

# Read config file
config = configparser.ConfigParser()
config.read('config.ini')

skipSynthesize = parseBool(config['SETTINGS']['skip_synthesize'])  # Set to true if you don't want to synthesize the audio. For example, you already did that and are testing
debugMode = parseBool(config['SETTINGS']['debug_mode'])

# Translation Settings
skipTranslation = parseBool(config['SETTINGS']['skip_translation'])  # Set to true if you don't want to translate the subtitles. If so, ignore the following two variables
originalLanguage = config['SETTINGS']['original_language']

# Note! Setting this to true will make it so instead of just stretching the audio clips, it will have the API generate new audio clips with adjusted speaking rates
# This can't be done on the first pass because we don't know how long the audio clips will be until we generate them
twoPassVoiceSynth = parseBool(config['SETTINGS']['two_pass_voice_synth'])

# Will add this many milliseconds of extra silence before and after each audio clip / spoken subtitle line
addBufferMilliseconds = int(config['SETTINGS']['add_line_buffer_milliseconds'])

# Will combine subtitles into one audio clip if they are less than this many characters
combineMaxChars = int(config['SETTINGS']['combine_subtitles_max_chars'])  

#---------------------------------------- Parse Cloud Service Settings ----------------------------------------
# Get auth and project settings for Azure or Google Cloud
cloudConfig = configparser.ConfigParser()
cloudConfig.read('cloud_service_settings.ini')
tts_service = cloudConfig['CLOUD']['tts_service']
googleProjectID = cloudConfig['CLOUD']['google_project_id']
batchSynthesize = parseBool(cloudConfig['CLOUD']['batch_tts_synthesize'])

#---------------------------------------- Batch File Processing ----------------------------------------

batchConfig = configparser.ConfigParser()
batchConfig.read('batch.ini')
# Get list of languages to process
languageNums = batchConfig['SETTINGS']['enabled_languages'].replace(' ','').split(',')
originalVideoFile = os.path.abspath(batchConfig['SETTINGS']['original_video_file_path'].strip("\""))
srtFile = os.path.abspath(batchConfig['SETTINGS']['srt_file_path'].strip("\""))

# Validate the number of sections
for num in languageNums:
    # Check if section exists
    if not batchConfig.has_section(f'LANGUAGE-{num}'):
        raise ValueError(f'Invalid language number in batch.ini: {num} - Make sure the section [LANGUAGE-{num}] exists')

# Validate the settings in each section
for num in languageNums:
    if not batchConfig.has_option(f'LANGUAGE-{num}', 'synth_language_code'):
        raise ValueError(f'Invalid configuration in batch.ini: {num} - Make sure the option "synth_language_code" exists under [LANGUAGE-{num}]')
    if not batchConfig.has_option(f'LANGUAGE-{num}', 'synth_voice_name'):
        raise ValueError(f'Invalid configuration in batch.ini: {num} - Make sure the option "synth_voice_name" exists under [LANGUAGE-{num}]')
    if not batchConfig.has_option(f'LANGUAGE-{num}', 'translation_target_language'):
        raise ValueError(f'Invalid configuration in batch.ini: {num} - Make sure the option "translation_target_language" exists under [LANGUAGE-{num}]')
    if not batchConfig.has_option(f'LANGUAGE-{num}', 'synth_voice_gender'):
        raise ValueError(f'Invalid configuration in batch.ini: {num} - Make sure the option "synth_voice_gender" exists under [LANGUAGE-{num}]')    

# Create a dictionary of the settings from each section
batchSettings = {}
for num in languageNums:
    batchSettings[num] = {
        'synth_language_code': batchConfig[f'LANGUAGE-{num}']['synth_language_code'],
        'synth_voice_name': batchConfig[f'LANGUAGE-{num}']['synth_voice_name'],
        'translation_target_language': batchConfig[f'LANGUAGE-{num}']['translation_target_language'],
        'synth_voice_gender': batchConfig[f'LANGUAGE-{num}']['synth_voice_gender']
    }

#======================================== Get Total Duration ================================================
# Final audio file Should equal the length of the video in milliseconds
def get_duration(filename):
    import subprocess, json
    result = subprocess.check_output(
            f'ffprobe -v quiet -show_streams -select_streams v:0 -of json "{filename}"', shell=True).decode()
    fields = json.loads(result)['streams'][0]
    try:
        duration = fields['tags']['DURATION']
    except KeyError:
        duration = fields['duration']
    durationMS = round(float(duration)*1000) # Convert to milliseconds
    return durationMS

totalAudioLength = get_duration(originalVideoFile)
#totalAudioLength = 999999 # Or set manually here and comment out the above line

#======================================== Parse SRT File ================================================
# Open an srt file and read the lines into a list
with open(srtFile, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Matches the following example with regex:    00:00:20,130 --> 00:00:23,419
subtitleTimeLineRegex = re.compile(r'\d\d:\d\d:\d\d,\d\d\d --> \d\d:\d\d:\d\d,\d\d\d')

# Create a dictionary
subsDict = {}

# Enumerate lines, and if a line in lines contains only an integer, put that number in the key, and a dictionary in the value
# The dictionary contains the start, ending, and duration of the subtitles as well as the text
# The next line uses the syntax HH:MM:SS,MMM --> HH:MM:SS,MMM . Get the difference between the two times and put that in the dictionary
# For the line after that, put the text in the dictionary
for lineNum, line in enumerate(lines):
    line = line.strip()
    if line.isdigit() and subtitleTimeLineRegex.match(lines[lineNum + 1]):
        lineWithTimestamps = lines[lineNum + 1].strip()
        lineWithSubtitleText = lines[lineNum + 2].strip()

        # If there are more lines after the subtitle text, add them to the text
        count = 3
        while True:
            # Check if the next line is blank or not
            if (lineNum+count) < len(lines) and lines[lineNum + count].strip():
                lineWithSubtitleText += ' ' + lines[lineNum + count].strip()
                count += 1
            else:
                break

        # Create empty dictionary with keys for start and end times and subtitle text
        subsDict[line] = {'start_ms': '', 'end_ms': '', 'duration_ms': '', 'text': '', 'break_until_next': '', 'srt_timestamps_line': lineWithTimestamps}

        time = lineWithTimestamps.split(' --> ')
        time1 = time[0].split(':')
        time2 = time[1].split(':')

        # Converts the time to milliseconds
        processedTime1 = int(time1[0]) * 3600000 + int(time1[1]) * 60000 + int(time1[2].split(',')[0]) * 1000 + int(time1[2].split(',')[1]) #/ 1000 #Uncomment to turn into seconds
        processedTime2 = int(time2[0]) * 3600000 + int(time2[1]) * 60000 + int(time2[2].split(',')[0]) * 1000 + int(time2[2].split(',')[1]) #/ 1000 #Uncomment to turn into seconds
        timeDifferenceMs = str(processedTime2 - processedTime1)

        # Adjust times with buffer
        if addBufferMilliseconds > 0:
            subsDict[line]['start_ms_buffered'] = str(processedTime1 + addBufferMilliseconds)
            subsDict[line]['end_ms_buffered'] = str(processedTime2 - addBufferMilliseconds)
            subsDict[line]['duration_ms_buffered'] = str((processedTime2 - addBufferMilliseconds) - (processedTime1 + addBufferMilliseconds))
        else:
            subsDict[line]['start_ms_buffered'] = str(processedTime1)
            subsDict[line]['end_ms_buffered'] = str(processedTime2)
            subsDict[line]['duration_ms_buffered'] = str(processedTime2 - processedTime1)
        
        # Set the keys in the dictionary to the values
        subsDict[line]['start_ms'] = str(processedTime1)
        subsDict[line]['end_ms'] = str(processedTime2)
        subsDict[line]['duration_ms'] = timeDifferenceMs
        subsDict[line]['text'] = lineWithSubtitleText
        if lineNum > 0:
            # Goes back to previous line's dictionary and writes difference in time to current line
            subsDict[str(int(line)-1)]['break_until_next'] = processedTime1 - int(subsDict[str(int(line) - 1)]['end_ms'])
        else:
            subsDict[line]['break_until_next'] = 0


#----------------------------------------------------------------------
def combine_subtitles_advanced(inputDict, maxCharacters=200):
    charRateGoal = 20 #20
    gapThreshold = 100 # The maximum gap between subtitles to combine

    # Convert dictionary to list of dictionaries of the values
    entryList = []
    for key, value in inputDict.items():
        value['originalIndex'] = int(key)-1
        entryList.append(value)
    
    def combine_single_pass(entryListLocal):
        # Want to restart the loop if a change is made, so use this variable, otherwise break only if the end is reached
        reachedEndOfList = False

        # Use while loop because the list is being modified
        while not reachedEndOfList:
            # Need to calculate the char_rate for each entry, any time something changes, so put it at the top of this loop
            entryListLocal = calc_list_speaking_rates(entryListLocal, charRateGoal)

            # Sort the list by the difference in speaking speed from charRateGoal
            priorityOrderedList = sorted(entryListLocal, key=itemgetter('char_rate_diff'), reverse=True) 

            # Iterates through the list in order of priority, and uses that index to operate on entryListLocal
            # For loop is broken after a combination is made, so that the list can be re-sorted and re-iterated
            for i, data in enumerate(priorityOrderedList):

                # Check if last entry, and therefore will end loop when done with this iteration
                if i == len(priorityOrderedList) - 1:
                    reachedEndOfList = True

                # Check if the current entry is outside the upper and lower bounds
                if (data['char_rate'] > charRateGoal or data['char_rate'] < charRateGoal):

                    # Set flags for whether to consider the next and previous entries
                    considerNext = True
                    considerPrev = True

                    # Get the char_rate of the next and previous entries, if they exist, and calculate the difference
                    # If the diff is positive, then it is lower than the current char_rate
                    try:
                        nextCharRate = entryListLocal[i+1]['char_rate']
                        nextDiff = data['char_rate'] - nextCharRate
                    except IndexError:
                        considerNext = False
                        nextCharRate = None
                        nextDiff = None
                        reachedEndOfList = True
                    try:
                        prevCharRate = entryListLocal[i-1]['char_rate']
                        prevDiff = data['char_rate'] - prevCharRate
                    except IndexError:
                        considerPrev = False
                        prevCharRate = None
                        prevDiff = None
                        
                else:
                    continue

                # Define functions for combining with previous or next entries - Generated with copilot, it's possible this isn't perfect
                def combine_with_next():
                    entryListLocal[i]['text'] = entryListLocal[i]['text'] + ' ' + entryListLocal[i+1]['text']
                    entryListLocal[i]['translated_text'] = entryListLocal[i]['translated_text'] + ' ' + entryListLocal[i+1]['translated_text']
                    entryListLocal[i]['end_ms'] = entryListLocal[i+1]['end_ms']
                    entryListLocal[i]['end_ms_buffered'] = entryListLocal[i+1]['end_ms_buffered']
                    entryListLocal[i]['duration_ms'] = int(entryListLocal[i+1]['end_ms']) - int(entryListLocal[i]['start_ms'])
                    entryListLocal[i]['duration_ms_buffered'] = int(entryListLocal[i+1]['end_ms_buffered']) - int(entryListLocal[i]['start_ms_buffered'])
                    entryListLocal[i]['srt_timestamps_line'] = entryListLocal[i]['srt_timestamps_line'].split(' --> ')[0] + ' --> ' + entryListLocal[i+1]['srt_timestamps_line'].split(' --> ')[1]
                    del entryListLocal[i+1]

                def combine_with_prev():
                    entryListLocal[i-1]['text'] = entryListLocal[i-1]['text'] + ' ' + entryListLocal[i]['text']
                    entryListLocal[i-1]['translated_text'] = entryListLocal[i-1]['translated_text'] + ' ' + entryListLocal[i]['translated_text']
                    entryListLocal[i-1]['end_ms'] = entryListLocal[i]['end_ms']
                    entryListLocal[i-1]['end_ms_buffered'] = entryListLocal[i]['end_ms_buffered']
                    entryListLocal[i-1]['duration_ms'] = int(entryListLocal[i]['end_ms']) - int(entryListLocal[i-1]['start_ms'])
                    entryListLocal[i-1]['duration_ms_buffered'] = int(entryListLocal[i]['end_ms_buffered']) - int(entryListLocal[i-1]['start_ms_buffered'])
                    entryListLocal[i-1]['srt_timestamps_line'] = entryListLocal[i-1]['srt_timestamps_line'].split(' --> ')[0] + ' --> ' + entryListLocal[i]['srt_timestamps_line'].split(' --> ')[1]
                    del entryListLocal[i]


                # Choose whether to consider next and previous entries, and if neither then continue to next loop
                if data['char_rate'] > charRateGoal:
                    # Check to ensure next/previous rates are lower than current rate, and the combined entry is not too long, and the gap between entries is not too large
                    if not nextDiff or nextDiff < 0 or (entryListLocal[i]['break_until_next'] >= gapThreshold) or (len(entryListLocal[i]['translated_text']) + len(entryListLocal[i+1]['translated_text']) > maxCharacters):
                        considerNext = False
                    try:
                        if not prevDiff or prevDiff < 0 or (entryListLocal[i-1]['break_until_next'] >= gapThreshold) or (len(entryListLocal[i-1]['translated_text']) + len(entryListLocal[i]['translated_text']) > maxCharacters):
                            considerPrev = False
                    except TypeError:
                        considerPrev = False

                elif data['char_rate'] < charRateGoal:
                    # Check to ensure next/previous rates are higher than current rate
                    if not nextDiff or nextDiff > 0 or (entryListLocal[i]['break_until_next'] >= gapThreshold) or (len(entryListLocal[i]['translated_text']) + len(entryListLocal[i+1]['translated_text']) > maxCharacters):
                        considerNext = False
                    try:
                        if not prevDiff or prevDiff > 0 or (entryListLocal[i-1]['break_until_next'] >= gapThreshold) or (len(entryListLocal[i-1]['translated_text']) + len(entryListLocal[i]['translated_text']) > maxCharacters):
                            considerPrev = False
                    except TypeError:
                        considerPrev = False
                else:
                    continue

                # Continue to next loop if neither are considered
                if not considerNext and not considerPrev:
                    continue

                # Should only reach this point if two entries are to be combined
                if data['char_rate'] > charRateGoal:
                    # If both are to be considered, then choose the one with the lower char_rate
                    if considerNext and considerPrev:
                        if nextDiff < prevDiff:
                            combine_with_next()
                            break
                        else:
                            combine_with_prev()
                            break
                    # If only one is to be considered, then combine with that one
                    elif considerNext:
                        combine_with_next()
                        break
                    elif considerPrev:
                        combine_with_prev()
                        break
                    else:
                        print(f"Error U: Should not reach this point! Current entry = {i}")
                        print(f"Current Entry Text = {data['text']}")
                        continue
                
                elif data['char_rate'] < charRateGoal:
                    # If both are to be considered, then choose the one with the higher char_rate
                    if considerNext and considerPrev:
                        if nextDiff > prevDiff:
                            combine_with_next()
                            break
                        else:
                            combine_with_prev()
                            break
                    # If only one is to be considered, then combine with that one
                    elif considerNext:
                        combine_with_next()
                        break
                    elif considerPrev:
                        combine_with_prev()
                        break
                    else:
                        print(f"Error L: Should not reach this point! Index = {i}")
                        print(f"Current Entry Text = {data['text']}")
                        continue
        return entryListLocal

    #-- End of combine_single_pass --

    # Two passes since they're combined sequentially in pairs. Might add a better way in the future
    # Need to create new list variable or else it won't update entryList if that is used for some reason
    entryList2 = combine_single_pass(entryList)
    entryList3 = combine_single_pass(entryList2)

    # Convert the list back to a dictionary then return it
    return dict(enumerate(entryList3, start=1))

#----------------------------------------------------------------------

# Calculate the number of characters per second for each subtitle entry
def calc_dict_speaking_rates(inputDict, dictKey='translated_text'):  
    tempDict = copy.deepcopy(inputDict)
    for key, value in tempDict.items():
        tempDict[key]['char_rate'] = round(len(value[dictKey]) / (int(value['duration_ms']) / 1000), 2)
    return tempDict

def calc_list_speaking_rates(inputList, charRateGoal, dictKey='translated_text'): 
    tempList = copy.deepcopy(inputList)
    for i in range(len(tempList)):
        # Calculate the number of characters per second based on the duration of the entry
        tempList[i]['char_rate'] = round(len(tempList[i][dictKey]) / (int(tempList[i]['duration_ms']) / 1000), 2)
        # Calculate the difference between the current char_rate and the goal char_rate - Absolute Value
        tempList[i]['char_rate_diff'] = abs(round(tempList[i]['char_rate'] - charRateGoal, 2))
    return tempList

# Apply the buffer to the start and end times by setting copying over the buffer values to main values
for key, value in subsDict.items():
    if addBufferMilliseconds > 0:
        subsDict[key]['start_ms'] = value['start_ms_buffered']
        subsDict[key]['end_ms'] = value['end_ms_buffered']
        subsDict[key]['duration_ms'] = value['duration_ms_buffered']

#======================================== Translate Text ================================================
# Note: This function was almost entirely written by GPT-3 after feeding it my original code and asking it to change it so it
# would break up the text into chunks if it was too long. It appears to work

# Translate the text entries of the dictionary
def translate_dictionary(inputSubsDict, langDict, skipTranslation=False):
    targetLanguage = langDict['targetLanguage']

    # Create a container for all the text to be translated
    textToTranslate = []

    for key in inputSubsDict:
        originalText = inputSubsDict[key]['text']
        textToTranslate.append(originalText)
    
    # Calculate the total number of utf-8 codepoints
    codepoints = 0
    for text in textToTranslate:
        codepoints += len(text.encode("utf-8"))
    
    # If the codepoints are greater than 28000, split the request into multiple
    # Google's API limit is 30000 Utf-8 codepoints per request, but we leave some room just in case
    if skipTranslation == False:
        if codepoints > 27000:
            # GPT-3 Description of what the following line does:
            # Splits the list of text to be translated into smaller chunks of 100 texts.
            # It does this by looping over the list in steps of 100, and slicing out each chunk from the original list. 
            # Each chunk is appended to a new list, chunkedTexts, which then contains the text to be translated in chunks.
            chunkedTexts = [textToTranslate[x:x+100] for x in range(0, len(textToTranslate), 100)]
            
            # Send and receive the batch requests
            for chunk in chunkedTexts:
                # Print status with progress
                print(f'Translating text group {chunkedTexts.index(chunk)+1} of {len(chunkedTexts)}')
                
                # Send the request
                response = auth.TRANSLATE_API.projects().translateText(
                    parent='projects/' + googleProjectID,
                    body={
                        'contents': chunk,
                        'sourceLanguageCode': originalLanguage,
                        'targetLanguageCode': targetLanguage,
                        'mimeType': 'text/plain',
                        #'model': 'nmt',
                        #'glossaryConfig': {}
                    }
                ).execute()

                # Extract the translated texts from the response
                translatedTexts = [response['translations'][i]['translatedText'] for i in range(len(response['translations']))]
                
                # Add the translated texts to the dictionary
                for i, key in enumerate(inputSubsDict):
                    inputSubsDict[key]['translated_text'] = translatedTexts[i]
                    # Print progress, ovwerwrite the same line
                    print(f' Translated: {key} of {len(inputSubsDict)}', end='\r')
        
        else:
            print("Translating text...")
            response = auth.TRANSLATE_API.projects().translateText(
                parent='projects/' + googleProjectID,
                body={
                    'contents':textToTranslate,
                    'sourceLanguageCode': originalLanguage,
                    'targetLanguageCode': targetLanguage,
                    'mimeType': 'text/plain',
                    #'model': 'nmt',
                    #'glossaryConfig': {}
                }
            ).execute()
            translatedTexts = [response['translations'][i]['translatedText'] for i in range(len(response['translations']))]
            for i, key in enumerate(inputSubsDict):
                inputSubsDict[key]['translated_text'] = translatedTexts[i]
                # Print progress, ovwerwrite the same line
                print(f' Translated: {key} of {len(inputSubsDict)}', end='\r')
    else:
        for key in inputSubsDict:
            inputSubsDict[key]['translated_text'] = inputSubsDict[key]['text'] # Skips translating, such as for testing
    print("                                                  ")

    combinedProcessedDict = combine_subtitles_advanced(inputSubsDict, combineMaxChars)

    if skipTranslation == False or debugMode == True:
        # Use video file name to use in the name of the translate srt file, also display regular language name
        lang = langcodes.get(targetLanguage).display_name()
        if debugMode:
            translatedSrtFileName = pathlib.Path(originalVideoFile).stem + f" - {lang} - {targetLanguage}.DEBUG.txt"
        else:
            translatedSrtFileName = pathlib.Path(originalVideoFile).stem + f" - {lang} - {targetLanguage}.srt"
        # Set path to save translated srt file
        translatedSrtFileName = os.path.join(outputFolder, translatedSrtFileName)
        # Write new srt file with translated text
        with open(translatedSrtFileName, 'w', encoding='utf-8') as f:
            for key in combinedProcessedDict:
                f.write(str(key) + '\n')
                f.write(combinedProcessedDict[key]['srt_timestamps_line'] + '\n')
                f.write(combinedProcessedDict[key]['translated_text'] + '\n')
                if debugMode:
                    f.write(f"DEBUG: duration_ms = {combinedProcessedDict[key]['duration_ms']}" + '\n')
                    f.write(f"DEBUG: char_rate = {combinedProcessedDict[key]['char_rate']}" + '\n')
                    f.write(f"DEBUG: start_ms = {combinedProcessedDict[key]['start_ms']}" + '\n')
                    f.write(f"DEBUG: end_ms = {combinedProcessedDict[key]['end_ms']}" + '\n')
                    f.write(f"DEBUG: start_ms_buffered = {combinedProcessedDict[key]['start_ms_buffered']}" + '\n')
                    f.write(f"DEBUG: end_ms_buffered = {combinedProcessedDict[key]['end_ms_buffered']}" + '\n')
                f.write('\n')

    return combinedProcessedDict

#============================================= Directory Validation =====================================================

# Check if the output folder exists, if not, create it
if not os.path.exists(outputFolder):
    os.makedirs(outputFolder)

# Check if the working folder exists, if not, create it
if not os.path.exists('workingFolder'):
    os.makedirs('workingFolder')

#======================================== Translation and Text-To-Speech ================================================    

# Create dictionary to store settings for the language to pass into functions
langDict = {}
for langNum, value in batchSettings.items():
    # Place settings into individual dictionary
    langDict = {
        'targetLanguage': value['translation_target_language'], 
        'voiceName': value['synth_voice_name'], 
        'languageCode': value['synth_language_code'], 
        'voiceGender': value['synth_voice_gender']
        }

    # Create subs dict to use for this language
    individualLanguageSubsDict = copy.deepcopy(subsDict)

    # Print language being processed
    print(f"\n----- Beginning Processing of Language: {langDict['languageCode']} -----")

    # Translate
    individualLanguageSubsDict = translate_dictionary(individualLanguageSubsDict, langDict, skipTranslation=skipTranslation)

    # Synthesize
    if batchSynthesize == True and tts_service == 'azure':
        individualLanguageSubsDict = TTS.synthesize_dictionary_batch(individualLanguageSubsDict, langDict, skipSynthesize=skipSynthesize)
    else:
        individualLanguageSubsDict = TTS.synthesize_dictionary(individualLanguageSubsDict, langDict, skipSynthesize=skipSynthesize)

    # Build audio
    individualLanguageSubsDict = audio_builder.build_audio(individualLanguageSubsDict, langDict, totalAudioLength, twoPassVoiceSynth)

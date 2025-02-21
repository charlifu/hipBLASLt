# ########################################################################
# Copyright (C) 2022 Advanced Micro Devices, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
# ########################################################################

# Set up Tensile Dependency
if( BUILD_WITH_TENSILE )
  # If we want to build a shared rocblaslt lib, force Tensile to build as a static lib to absorb into rocblaslt
  if( BUILD_SHARED_LIBS )
    set( ROCBLASLT_SHARED_LIBS ON )
    set( BUILD_SHARED_LIBS OFF )
  else( )
    set( ROCBLASLT_SHARED_LIBS OFF )
  endif( )

  set( Tensile_RUNTIME_LANGUAGE "HIP" )
  #set( Tensile_LIBRARY_FORMAT "yaml" )

  #TODO update when this feature has been validated
  #set( PACKAGE_TENSILE_LIBRARY ON )
  set( PACKAGE_TENSILE_LIBRARY OFF )

  # Build options list
  if(Tensile_MERGE_FILES)
    set(Tensile_Options ${Tensile_Options} MERGE_FILES)
  endif()
  if(Tensile_SHORT_FILENAMES)
    set(Tensile_Options ${Tensile_Options} SHORT_FILE_NAMES)
  endif()
  if(Tensile_PRINT_DEBUG)
    set(Tensile_Options ${Tensile_Options} PRINT_DEBUG)
  endif()
  if(PACKAGE_TENSILE_LIBRARY)
    set(Tensile_Options ${Tensile_Options} GENERATE_PACKAGE)
  endif()

  # Add a build target for Tensile kernel library
  # Runtime language is HIP by default
  # warning our Tensile_ variables may shadow variable in TensileCreateLibraryFiles
  # thus bypassing the function argument parameter system (mainly the options list) and CPU_THREADS
  if(Tensile_CPU_THREADS MATCHES "^[0-9]+$")
    # only including threads argument if number
    TensileCreateLibraryFiles(
      "${CMAKE_CURRENT_SOURCE_DIR}/src/amd_detail/rocblaslt/src/Tensile/Logic/${Tensile_LOGIC}"
      "${PROJECT_BINARY_DIR}/Tensile"
      ARCHITECTURE        ${Tensile_ARCHITECTURE}
      CODE_OBJECT_VERSION ${Tensile_CODE_OBJECT_VERSION}
      COMPILER            ${Tensile_COMPILER}
      LIBRARY_FORMAT      ${Tensile_LIBRARY_FORMAT}
      CPU_THREADS         ${Tensile_CPU_THREADS}
      ${Tensile_Options}
    )
  else()
    TensileCreateLibraryFiles(
      "${CMAKE_CURRENT_SOURCE_DIR}/src/amd_detail/rocblaslt/src/Tensile/Logic/${Tensile_LOGIC}"
      "${PROJECT_BINARY_DIR}/Tensile"
      ARCHITECTURE        ${Tensile_ARCHITECTURE}
      CODE_OBJECT_VERSION ${Tensile_CODE_OBJECT_VERSION}
      COMPILER            ${Tensile_COMPILER}
      LIBRARY_FORMAT      ${Tensile_LIBRARY_FORMAT}
      ${Tensile_Options}
    )
  endif()

  TensileCreateExtOpLibraries("${PROJECT_BINARY_DIR}/Tensile/library" "${Tensile_ARCHITECTURE}")

  # Create a unique name for TensileHost compiled for rocBLAS
  set_target_properties( TensileHost PROPERTIES OUTPUT_NAME rocblaslt-tensile CXX_EXTENSIONS NO )

  # Tensile host depends on libs build target
  add_dependencies( TensileHost TENSILE_LIBRARY_TARGET )

  if( ROCBLASLT_SHARED_LIBS )
    set( BUILD_SHARED_LIBS ON )
    set_target_properties( TensileHost PROPERTIES POSITION_INDEPENDENT_CODE ON )
  endif()

  set( Tensile_SRC
    src/amd_detail/rocblaslt/src/tensile_host.cpp
  )

  set( Tensile_INC
    ${CMAKE_CURRENT_SOURCE_DIR}/src/amd_detail/rocblaslt/src/Tensile
  )

endif( ) # BUILD_WITH_TENSILE

set(DL_LIB dl)

# rocBLASLt source
set(rocblaslt_source
  src/amd_detail/rocblaslt/src/handle.cpp
  src/amd_detail/rocblaslt/src/status.cpp
  src/amd_detail/rocblaslt/src/rocblaslt_auxiliary.cpp
  src/amd_detail/rocblaslt/src/rocblaslt_mat.cpp
  src/amd_detail/rocblaslt/src/utility.cpp
  src/amd_detail/rocblaslt/src/rocblaslt_transform.cpp
  ${Tensile_SRC}
)
